"""Push to talk: hold a key (or the button), speak, release; the words are recognised and read
aloud in a cloned voice.

start() opens the microphone (listen.input_device, default device when unset) and buffers 16 kHz
audio; stop() closes it, keeps only the speech (Silero VAD, a little padding), recognises it with
SenseVoice on the CPU (language detected: zh / en / ja / ko / yue) and, when asked, speaks the text
with the chosen voice through the local player. Progress is published as "ptt" events.
simulate() feeds an audio file instead of the microphone (tests, or no microphone attached).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import numpy as np

from . import asr, audio_util, config
from .events import bus

log = logging.getLogger("charavoice.ptt")
SR = 16000
MAX_SECONDS = 60


class _Take:
    def __init__(self) -> None:
        self.chunks: list[np.ndarray] = []
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self.started = time.monotonic()
        self.source = "mic"


_take: _Take | None = None
_lock = threading.Lock()


def _mic(take: _Take) -> None:
    import sounddevice as sd

    device = config.load_settings()["listen"]["input_device"]
    step = int(SR * 0.03)
    try:
        if device is None and sd.default.device[0] < 0:
            raise RuntimeError("没有找到可用的麦克风：Windows 里没有设置默认录音设备。请接上麦克风，或在“设置”里选择麦克风。")
        with sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=step, device=device) as stream:
            while not take.stop.is_set() and time.monotonic() - take.started < MAX_SECONDS:
                data, _ = stream.read(step)
                take.chunks.append(data.reshape(-1).copy())
                bus.publish("level", source="mic", level=round(float(np.sqrt(np.mean(data ** 2)) * 6), 3))
    except Exception as e:  # PortAudioError, no device, Windows privacy setting blocks the microphone
        take.error = f"无法打开麦克风：{e}。请检查麦克风是否接好，以及 Windows“设置 → 隐私和安全性 → 麦克风”是否允许桌面应用使用麦克风。"
        log.warning("ptt microphone failed: %s", e)


def start() -> dict[str, Any]:
    global _take
    with _lock:
        if _take and not _take.stop.is_set():
            return {"recording": True}
        _take = _Take()
        _take.thread = threading.Thread(target=_mic, args=(_take,), daemon=True, name="ptt")
        _take.thread.start()
    bus.publish("ptt", state="recording")
    return {"recording": True}


def simulate(path: str) -> dict[str, Any]:
    """Act as if the user held the key and said the content of an audio file."""
    global _take
    with _lock:
        _take = _Take()
        _take.source = "file"
        _take.chunks = [audio_util.decode(path, SR)]
    bus.publish("ptt", state="recording")
    return {"recording": True, "seconds": round(len(_take.chunks[0]) / SR, 2)}


def _speech_only(x: np.ndarray) -> np.ndarray:
    """Keep the voiced parts (Silero VAD) with 0.15 s padding; the whole take if VAD finds nothing."""
    try:
        segs = asr.segment(x, min_silence=0.3, min_speech=0.2, max_speech=20.0)
    except Exception:
        return x
    if not segs:
        return np.zeros(0, np.float32)
    pad = int(0.15 * SR)
    a = max(0, segs[0][0] - pad)
    b = min(len(x), segs[-1][0] + len(segs[-1][1]) + pad)
    return x[a:b]


def stop() -> dict[str, Any]:
    """End the take and recognise it. Returns text, language and timings."""
    global _take
    with _lock:
        take, _take = _take, None
    if not take:
        raise ValueError("当前没有在按住说话。")
    take.stop.set()
    if take.thread:
        take.thread.join(2)
    t_release = time.monotonic()
    if take.error:
        bus.publish("ptt", state="error", message=take.error)
        raise RuntimeError(take.error)
    x = np.concatenate(take.chunks) if take.chunks else np.zeros(0, np.float32)
    bus.publish("ptt", state="recognizing", seconds=round(len(x) / SR, 2))
    speech = _speech_only(x)
    if len(speech) < SR * 0.2:
        out = {"text": "", "language": "", "seconds": round(len(x) / SR, 2), "asr_ms": 0}
        bus.publish("ptt", state="empty", **out)
        return out
    r = asr.transcribe(speech)
    out = {"text": r["text"], "language": r["language"], "seconds": round(len(x) / SR, 2),
           "asr_ms": int((time.monotonic() - t_release) * 1000), "source": take.source}
    bus.publish("ptt", state="text", **out)
    return out
