"""Speech recognition (sherpa-onnx, CPU): SenseVoice by default, Whisper optional.

Two entry points:
  * transcribe(samples)  - offline recognition of a 16 kHz clip
  * listener             - microphone (or file) -> Silero VAD -> recognizer,
                           publishing speech_start / speech_end / transcript /
                           level events on the bus
"""

from __future__ import annotations

import itertools
import logging
import queue
import re
import threading
import time
from pathlib import Path

import numpy as np

from . import config
from .events import LISTENING, bus
from .player import level_of, player

log = logging.getLogger("charavoice.asr")
SR = 16000
_TAG = re.compile(r"<\|([^|]*)\|>")
# SenseVoice sometimes emits spaces between Japanese/Chinese words; they are not part of the text.
_CJK = r"぀-ヿ㐀-䶿一-鿿＀-￯　-〿"
_CJK_GAP = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])")
_rec_lock = threading.Lock()
_recognizers: dict[tuple, object] = {}


def _strip(tag: str) -> str:
    m = _TAG.fullmatch(tag or "")
    return (m.group(1) if m else tag or "").lower()


def _recognizer(language: str = ""):
    import sherpa_onnx

    s = config.load_settings()["listen"]
    key = (s["engine"], language)
    with _rec_lock:
        if key in _recognizers:
            return _recognizers[key]
        if s["engine"] == "whisper":
            d = config.resolve(s["whisper_dir"])
            p = s["whisper_prefix"]
            files = [d / f"{p}-encoder.int8.onnx", d / f"{p}-decoder.int8.onnx", d / f"{p}-tokens.txt"]
            _require(files, "Whisper")
            rec = sherpa_onnx.OfflineRecognizer.from_whisper(
                encoder=str(files[0]), decoder=str(files[1]), tokens=str(files[2]),
                language=language if language not in ("", "auto") else "",
                num_threads=int(s["num_threads"]),
            )
        else:
            d = config.resolve(s["sensevoice_dir"])
            files = [d / s["sensevoice_model"], d / "tokens.txt"]
            _require(files, "SenseVoice")
            rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(files[0]), tokens=str(files[1]), num_threads=int(s["num_threads"]),
                language=language if language in ("auto", "zh", "en", "ja", "ko", "yue") else "auto",
                use_itn=True,
            )
        _recognizers[key] = rec
        return rec


def _require(files: list[Path], name: str) -> None:
    missing = [f.name for f in files if not f.exists()]
    if missing:
        raise RuntimeError(f"缺少 {name} 模型文件：{', '.join(missing)}（请在“模型”页安装听写模型）")


def transcribe(samples: np.ndarray, language: str = "") -> dict:
    """Recognize a mono float32 16 kHz clip."""
    rec = _recognizer(language or "")
    stream = rec.create_stream()
    stream.accept_waveform(SR, samples.astype(np.float32))
    rec.decode_stream(stream)
    r = stream.result
    return {
        "text": _CJK_GAP.sub("", r.text.strip()),
        "language": _strip(getattr(r, "lang", "")) or (language if language not in ("", "auto") else ""),
        "emotion": _strip(getattr(r, "emotion", "")),
        "event": _strip(getattr(r, "event", "")),
        "duration": round(len(samples) / SR, 2),
    }


def segment(samples: np.ndarray, min_silence: float = 0.4, min_speech: float = 0.25, max_speech: float = 10.0) -> list[tuple[int, np.ndarray]]:
    """Split a long 16 kHz clip into speech segments with Silero VAD. Returns [(start_sample, samples)]."""
    vad = _make_vad(min_silence=min_silence, min_speech=min_speech, max_speech=max_speech)
    w = vad.config.silero_vad.window_size
    out = []
    for i in range(0, len(samples) - w + 1, w):
        vad.accept_waveform(samples[i : i + w])
        while not vad.empty():
            out.append((vad.front.start, np.array(vad.front.samples, dtype=np.float32)))
            vad.pop()
    vad.flush()
    while not vad.empty():
        out.append((vad.front.start, np.array(vad.front.samples, dtype=np.float32)))
        vad.pop()
    return out


def _make_vad(threshold: float | None = None, min_silence: float | None = None, min_speech: float | None = None, max_speech: float | None = None):
    import sherpa_onnx

    s = config.load_settings()["listen"]
    model = config.resolve(s["vad_model"])
    _require([model], "Silero VAD")
    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = str(model)
    cfg.silero_vad.threshold = float(threshold if threshold is not None else s["vad_threshold"])
    cfg.silero_vad.min_silence_duration = float(min_silence if min_silence is not None else s["min_silence"])
    cfg.silero_vad.min_speech_duration = float(min_speech if min_speech is not None else s["min_speech"])
    cfg.silero_vad.max_speech_duration = float(max_speech if max_speech is not None else s["max_speech"])
    cfg.sample_rate = SR
    cfg.num_threads = 1
    return sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)


class Listener:
    """Shared microphone pipeline; runs while at least one consumer holds it."""

    def __init__(self) -> None:
        self._users = 0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._sim: "queue.Queue[np.ndarray | None]" = queue.Queue()
        self._ids = itertools.count(1)
        self.source = "mic"
        self.error: str | None = None
        self._jobs: "queue.Queue[tuple[int, np.ndarray]]" = queue.Queue()
        self._partial_q: "queue.Queue[tuple[int, np.ndarray]]" = queue.Queue(maxsize=1)
        self._partial_seq = 0
        threading.Thread(target=self._recognize_loop, name="asr", daemon=True).start()
        threading.Thread(target=self._partial_loop, name="asr-partial", daemon=True).start()

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def acquire(self) -> None:
        with self._lock:
            self._users += 1
            if not self.active:
                self._stop.clear()
                self.error = None
                self._thread = threading.Thread(target=self._run, name="listener", daemon=True)
                self._thread.start()

    def release(self) -> None:
        with self._lock:
            self._users = max(0, self._users - 1)
            if self._users == 0:
                self._stop.set()

    def simulate(self, samples: np.ndarray) -> None:
        """Feed a 16 kHz clip through the pipeline in real time instead of the microphone."""
        step = int(SR * 0.03)
        for i in range(0, len(samples), step):
            self._sim.put(samples[i : i + step])
        self._sim.put(np.zeros(int(SR * 1.0), dtype=np.float32))

    def _blocks(self):
        """Yield 30 ms float32 blocks from the simulated queue or the microphone."""
        import sounddevice as sd

        step = int(SR * 0.03)
        if not self._sim.empty() or self.source == "file":
            self.source = "file"
            next_t = time.monotonic()
            while not self._stop.is_set():
                try:
                    b = self._sim.get(timeout=0.2)
                except queue.Empty:
                    yield np.zeros(step, dtype=np.float32)
                    continue
                next_t += len(b) / SR
                time.sleep(max(0.0, next_t - time.monotonic()))
                yield b
            return
        device = config.load_settings()["listen"]["input_device"]
        try:
            if device is None and sd.default.device[0] < 0:
                raise RuntimeError("没有找到可用的麦克风：Windows 里没有设置默认录音设备。请接上麦克风，或在“设置 → 麦克风”里选择一个设备。")
            stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=step, device=device)
        except sd.PortAudioError as e:
            raise RuntimeError(f"无法打开麦克风（{e}）。请检查设备是否被其他程序独占，或在“设置 → 麦克风”里换一个设备。") from None
        with stream:
            while not self._stop.is_set():
                data, _ = stream.read(step)
                yield data.reshape(-1)

    def _run(self) -> None:
        s = config.load_settings()["listen"]
        bus.enter(LISTENING)
        bus.publish("listen_start", source=self.source)
        try:
            vad = _make_vad()
            w = vad.config.silero_vad.window_size
            buf = np.zeros(0, dtype=np.float32)
            in_speech = False
            muted = False
            # partial results: re-recognise the utterance so far every PARTIAL_EVERY seconds
            pre = np.zeros(0, dtype=np.float32)  # ~0.4 s pre-roll so the first word is not clipped
            seg_pre = pre
            hist = np.zeros(0, dtype=np.float32)  # audio fed to the VAD, long enough to cover a whole segment
            hist_len = int(SR * (float(s["max_speech"]) + 2))
            fed = 0  # samples fed to the VAD so far (VAD segment start indices count from here)
            cur: list[np.ndarray] = []
            since_partial = 0
            partial_every = int(SR * float(s.get("partial_interval", 0.4)))
            for block in self._blocks():
                if s["pause_while_speaking"] and player.busy:
                    if not muted:
                        muted = True
                        vad.reset()
                        buf = np.zeros(0, dtype=np.float32)
                        hist, fed = np.zeros(0, dtype=np.float32), 0
                        if in_speech:
                            in_speech = False
                            bus.publish("speech_end", id=None, duration=0.0, discarded=True)
                    continue
                if muted:
                    muted = False
                bus.publish("level", source="mic", level=round(level_of(block), 3))
                if in_speech:
                    cur.append(block)
                    since_partial += len(block)
                    if partial_every and since_partial >= partial_every:
                        since_partial = 0
                        self._post_partial(np.concatenate(cur))
                else:
                    pre = np.concatenate([pre, block])[-int(SR * 0.4):]
                buf = np.concatenate([buf, block])
                while len(buf) >= w:
                    vad.accept_waveform(buf[:w])
                    hist = np.concatenate([hist, buf[:w]])[-hist_len:]
                    fed += w
                    buf = buf[w:]
                    speaking_now = vad.is_speech_detected()
                    if speaking_now and not in_speech:
                        in_speech = True
                        seg_pre = pre.copy()
                        cur, since_partial = [seg_pre], 0
                        bus.publish("speech_start")
                    while not vad.empty():
                        # VAD segments can start a little late; add up to 0.5 s of the audio just before
                        # the segment start (exact sample index, so nothing is duplicated)
                        st = int(vad.front.start)
                        lo = max(st - int(SR * 0.5), fed - len(hist))
                        pad = hist[lo - (fed - len(hist)) : max(lo, st) - (fed - len(hist))] if st > lo else hist[:0]
                        seg = np.concatenate([pad, np.array(vad.front.samples, dtype=np.float32)])
                        # how long ago the speech actually ended (the VAD needs min_silence to decide)
                        end_lag = (fed - (st + len(vad.front.samples))) / SR
                        vad.pop()
                        in_speech = vad.is_speech_detected()
                        cur, since_partial = ([pre.copy()] if in_speech else []), 0
                        self._partial_seq += 1  # drop any partial still queued for this utterance
                        sid = next(self._ids)
                        bus.publish("speech_end", id=sid, duration=round(len(seg) / SR, 2), end_lag=round(end_lag, 3))
                        self._jobs.put((sid, seg))
        except RuntimeError as e:
            log.warning("listener stopped: %s", e)
            self.error = str(e)
            bus.publish("listen_error", message=self.error)
        except Exception as e:
            log.exception("listener failed")
            self.error = f"麦克风听写失败：{e}"
            bus.publish("listen_error", message=self.error)
        finally:
            self.source = "mic"
            bus.publish("level", source="mic", level=0.0)
            bus.publish("listen_stop")
            bus.leave(LISTENING)

    def _post_partial(self, audio: np.ndarray) -> None:
        item = (self._partial_seq, audio)
        try:
            self._partial_q.put_nowait(item)
        except queue.Full:  # keep only the newest snapshot
            try:
                self._partial_q.get_nowait()
            except queue.Empty:
                pass
            self._partial_q.put_nowait(item)

    def _partial_loop(self) -> None:
        while True:
            seq, audio = self._partial_q.get()
            if seq != self._partial_seq:
                continue
            try:
                r = transcribe(audio)
                if r["text"] and seq == self._partial_seq:
                    bus.publish("partial", text=r["text"], language=r["language"], duration=r["duration"])
            except Exception:
                log.exception("partial recognition failed")

    def _recognize_loop(self) -> None:
        while True:
            sid, seg = self._jobs.get()
            try:
                t0 = time.perf_counter()
                r = transcribe(seg)
                r["asr_ms"] = round((time.perf_counter() - t0) * 1000)
                if r["text"]:
                    bus.publish("transcript", id=sid, **r)
                else:
                    bus.publish("transcript", id=sid, empty=True, **r)
            except Exception as e:
                log.exception("recognition failed")
                bus.publish("listen_error", message=f"识别失败：{e}")


listener = Listener()
