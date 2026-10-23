"""Local playback queue with live output level.

Utterances are played one after another on a dedicated thread. Each ~30 ms block
is measured right before it is written to the device, and its level is
published on the event bus so the UI ring follows the voice that is actually
coming out of the speakers, including when an agent calls the HTTP/MCP API.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import sounddevice as sd

from . import config
from .events import SPEAKING, bus

log = logging.getLogger("charavoice.player")
_END = object()
_ids = itertools.count(1)


@dataclass
class Utterance:
    voice: str
    text: str
    id: int = field(default_factory=lambda: next(_ids))
    chunks: "queue.Queue" = field(default_factory=queue.Queue)
    sr: int = 0
    done: threading.Event = field(default_factory=threading.Event)
    started: threading.Event = field(default_factory=threading.Event)
    cancelled: bool = False
    error: str | None = None
    played_seconds: float = 0.0

    def feed(self, sr: int, pcm: np.ndarray) -> None:
        self.sr = self.sr or sr
        self.chunks.put(pcm)

    def finish(self, error: str | None = None) -> None:
        self.error = error
        self.chunks.put(_END)


def level_of(block: np.ndarray) -> float:
    """Perceptual-ish 0..1 level from float samples (RMS in dBFS mapped from -50..-6 dB)."""
    if block.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))
    if rms <= 1e-6:
        return 0.0
    db = 20 * np.log10(rms)
    return float(np.clip((db + 50) / 44, 0.0, 1.0))


class Player:
    def __init__(self) -> None:
        self._q: "queue.Queue[Utterance]" = queue.Queue()
        self._current: Utterance | None = None
        self._stop_flag = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._pending = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._run, name="player", daemon=True).start()

    @property
    def busy(self) -> bool:
        return not self._idle.is_set()

    def wait_idle(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout)

    def enqueue(self, utt: Utterance) -> Utterance:
        with self._lock:
            self._pending += 1
            self._idle.clear()
        self._q.put(utt)
        return utt

    def stop_all(self) -> int:
        """Cancel the current utterance and everything queued. Returns how many were cancelled."""
        n = 0
        while True:
            try:
                u = self._q.get_nowait()
            except queue.Empty:
                break
            u.cancelled = True
            u.done.set()
            n += 1
            self._dec()
        cur = self._current
        if cur is not None:
            cur.cancelled = True
            n += 1
        return n

    def _dec(self) -> None:
        with self._lock:
            self._pending -= 1
            if self._pending <= 0:
                self._pending = 0
                self._idle.set()

    def _run(self) -> None:
        while True:
            utt = self._q.get()
            self._current = utt
            try:
                if not utt.cancelled:
                    self._play(utt)
            except Exception as e:  # device errors must not kill the thread
                log.exception("playback failed")
                utt.error = utt.error or f"播放失败：{e}"
            finally:
                self._current = None
                utt.done.set()
                self._dec()

    def _play(self, utt: Utterance) -> None:
        first = utt.chunks.get()
        if first is _END or utt.cancelled:
            return
        sr = utt.sr
        device = config.load_settings()["speech"]["output_device"]
        out_sr = sr
        try:
            sd.check_output_settings(device=device, samplerate=sr, channels=1, dtype="float32")
        except Exception:
            out_sr = int(sd.query_devices(device, "output")["default_samplerate"])
        block = int(out_sr * 0.03)
        bus.enter(SPEAKING)
        bus.publish("speak_start", id=utt.id, voice=utt.voice, text=utt.text)
        utt.started.set()
        t0 = time.monotonic()
        try:
            with sd.OutputStream(samplerate=out_sr, channels=1, dtype="float32", device=device, latency="low") as stream:
                lat = float(stream.latency)
                pending = np.zeros(0, dtype=np.float32)
                chunk = first
                while True:
                    if chunk is _END:
                        break
                    x = chunk.astype(np.float32) / 32768.0
                    if out_sr != sr:
                        x = _resample(x, sr, out_sr)
                    pending = np.concatenate([pending, x])
                    while len(pending) >= block:
                        if utt.cancelled:
                            return
                        b, pending = pending[:block], pending[block:]
                        self._emit_level(b, lat)
                        stream.write(b.reshape(-1, 1))
                        utt.played_seconds += len(b) / out_sr
                    chunk = utt.chunks.get()
                if len(pending) and not utt.cancelled:
                    self._emit_level(pending, lat)
                    stream.write(pending.reshape(-1, 1))
                    utt.played_seconds += len(pending) / out_sr
                # Let the device drain what is still buffered before closing.
                time.sleep(lat)
        finally:
            bus.publish("level", source="speak", level=0.0)
            bus.publish("speak_end", id=utt.id, cancelled=utt.cancelled, seconds=round(time.monotonic() - t0, 2))
            bus.leave(SPEAKING)

    def _emit_level(self, block: np.ndarray, latency: float) -> None:
        bus.publish("level", source="speak", level=round(level_of(block), 3), delay=round(latency, 3))


def _resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    n = int(round(len(x) * sr_out / sr_in))
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


player = Player()
