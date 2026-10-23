"""Record what the computer is playing (WASAPI loopback, via PyAudioWPatch).

PyAudioWPatch exposes every WASAPI output device a second time as a "[Loopback]"
input device. Loopback streams deliver no buffers while nothing is playing, so
the writer pads with silence according to wall-clock time to keep the timeline
true to what the user saw while recording.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from .events import bus
from .player import level_of

MAX_SECONDS = 3 * 3600


def devices() -> list[dict[str, Any]]:
    import pyaudiowpatch as pa

    with pa.PyAudio() as p:
        try:
            default = p.get_default_wasapi_loopback()["index"]
        except (OSError, LookupError):
            default = None
        out = []
        for d in p.get_loopback_device_info_generator():
            out.append({
                "index": d["index"],
                "name": d["name"].replace(" [Loopback]", ""),
                "channels": int(d["maxInputChannels"]),
                "rate": int(d["defaultSampleRate"]),
                "default": d["index"] == default,
            })
        return out


class Recording:
    def __init__(self, voice: str, path: Path, device: dict[str, Any]):
        self.id = uuid.uuid4().hex[:8]
        self.voice = voice
        self.path = path
        self.device = device
        self.started = time.time()
        self.seconds = 0.0
        self.peak = 0.0
        self.stopped = threading.Event()
        self.opened = threading.Event()
        self.error: str | None = None


class Recorder:
    def __init__(self) -> None:
        self.current: Recording | None = None
        self._lock = threading.Lock()

    def start(self, voice: str, out_dir: Path, device_index: int | None) -> Recording:
        """Open the loopback stream on the recording thread itself: PortAudio's WASAPI
        backend initialises COM per thread, so the PyAudio instance, the stream and the
        reads must all live on one thread (opening from another one fails with -9999)."""
        with self._lock:
            if self.current and not self.current.stopped.is_set():
                raise ValueError("已经在录音了，请先停止当前录音。")
            out_dir.mkdir(parents=True, exist_ok=True)
            rec = Recording(voice, out_dir / f"rec-{time.strftime('%Y%m%d-%H%M%S')}.wav", {"index": device_index})
            rec.opened = threading.Event()
            self.current = rec
        threading.Thread(target=self._run, args=(rec,), daemon=True, name="loopback").start()
        rec.opened.wait(10)
        if rec.error:
            raise ValueError(rec.error)
        return rec

    def stop(self) -> Recording | None:
        rec = self.current
        if rec:
            rec.stopped.set()
        return rec

    def _run(self, rec: Recording) -> None:
        import pyaudiowpatch as pa

        p = pa.PyAudio()
        try:
            if rec.device["index"] is None:
                info = p.get_default_wasapi_loopback()
            else:
                info = p.get_device_info_by_index(rec.device["index"])
                if not info.get("isLoopbackDevice"):
                    raise ValueError("选择的不是回环（录制电脑声音）设备。")
        except (OSError, LookupError, ValueError) as e:
            p.terminate()
            rec.error = f"找不到可录制的输出设备：{e}"
            rec.stopped.set()
            rec.opened.set()
            return
        rec.device = {"index": info["index"], "name": info["name"].replace(" [Loopback]", ""),
                      "channels": int(info["maxInputChannels"]), "rate": int(info["defaultSampleRate"])}
        ch, rate = rec.device["channels"], rec.device["rate"]
        chunks: list[np.ndarray] = []
        lock = threading.Lock()

        def cb(data, frames, time_info, status):
            with lock:
                chunks.append(np.frombuffer(data, dtype=np.int16).reshape(-1, ch).copy())
            return (None, pa.paContinue)

        written = 0
        last_pub = 0.0
        try:
            with sf.SoundFile(str(rec.path), "w", samplerate=rate, channels=ch, subtype="PCM_16") as f, p.open(
                format=pa.paInt16, channels=ch, rate=rate, frames_per_buffer=1024, input=True,
                input_device_index=rec.device["index"], stream_callback=cb,
            ):
                t0 = time.monotonic()
                rec.opened.set()
                bus.publish("rec_start", id=rec.id, voice=rec.voice, device=rec.device["name"])
                while not rec.stopped.wait(0.05):
                    with lock:
                        got, chunks[:] = list(chunks), []
                    block = np.concatenate(got) if got else np.zeros((0, ch), np.int16)
                    # silence padding: loopback delivers nothing while the device is idle
                    expected = int((time.monotonic() - t0) * rate)
                    gap = expected - written - len(block) - int(rate * 0.25)
                    if gap > 0 and not got:
                        block = np.zeros((gap, ch), np.int16)
                    if len(block):
                        f.write(block)
                        written += len(block)
                    rec.seconds = written / rate
                    lv = level_of(block.astype(np.float32).mean(axis=1) / 32768.0) if len(block) else 0.0
                    rec.peak = max(rec.peak * 0.9, lv)
                    now = time.monotonic()
                    if now - last_pub >= 0.05:
                        last_pub = now
                        bus.publish("rec_level", id=rec.id, level=round(lv, 3), seconds=round(rec.seconds, 2))
                    if rec.seconds >= MAX_SECONDS:
                        rec.error = "已达到单次录音上限（3 小时），自动停止。"
                        break
                with lock:
                    rest = np.concatenate(chunks) if chunks else None
                if rest is not None and len(rest):
                    f.write(rest)
                    written += len(rest)
                rec.seconds = written / rate
        except Exception as e:
            rec.error = f"录音失败：{e}"
        finally:
            p.terminate()
            rec.stopped.set()
            rec.opened.set()
            bus.publish("rec_stop", id=rec.id, seconds=round(rec.seconds, 2), error=rec.error)


recorder = Recorder()
