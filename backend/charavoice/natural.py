"""Making synthesized speech sound less mechanical.

Generation side
- Pauses follow punctuation instead of one fixed gap: the text is split into clauses here, each
  clause is synthesized on its own, the engine's own leading/trailing silence is trimmed, and a
  pause chosen by the clause's final punctuation (comma, sentence end, ellipsis, paragraph) with
  +-15 % jitter is inserted. This works the same while streaming.
- Breaths taken from the voice's own material (see breaths.py) are placed in some sentence-final
  pauses before longer sentences, at the speaker's natural breath level.
Audio side (streaming-safe, one `Post` per utterance)
- light 2:1 compression, de-essing (5-9 kHz band ducked only when it dominates), loudness
  normalised to about -16 LUFS (BS.1770 K-weighting, gain decided on the first clause and then
  tracked slowly), a very small synthetic room (RT60 ~0.3 s, wet about -26 dB) and a pink room tone
  around -62 dBFS so pauses are not digital silence, then a soft peak limiter at -1 dBFS.
Every part can be switched off; "off" restores the plain engine output.

Defaults (measured with UTMOS22 on identical audio, see Docs/Implementation.html): pauses and
loudness are on; breaths, dynamics and room are off because the MOS predictor scores each of them
slightly lower (-0.04..-0.10, all together -0.23). "Natural mode" switches everything on for users
who prefer the livelier sound; the blind A/B set lets them decide by ear.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

import numpy as np

DEFAULTS = {
    "pauses": True, "comma": 0.2, "sentence": 0.45, "paragraph": 0.8, "jitter": 0.15,
    "breaths": False, "breath_prob": 0.35,
    "dynamics": False, "loudness": True, "target_lufs": -16.0,
    "room": False, "room_tone_db": -62.0, "reverb_wet_db": -26.0, "rt60": 0.3,
}
OFF = {k: (False if isinstance(v, bool) else v) for k, v in DEFAULTS.items()}


def resolve(settings: dict | None, request: bool | str | dict | None) -> dict:
    """Saved settings, overridden by the request: true/"on" = natural mode, false/"off" = plain output,
    or a dict of individual switches."""
    base = {**DEFAULTS, **(settings or {})}
    if request is None:
        return base
    if request is True or request == "on":
        return {**base, **{k: True for k, v in DEFAULTS.items() if isinstance(v, bool)}}
    if request is False or request == "off":
        return {**base, **{k: False for k, v in DEFAULTS.items() if isinstance(v, bool)}}
    if isinstance(request, dict):
        return {**base, **{k: v for k, v in request.items() if k in DEFAULTS}}
    raise ValueError("naturalness 只能是 true / false / \"on\" / \"off\" 或开关字典")


def any_post(cfg: dict) -> bool:
    return bool(cfg.get("dynamics") or cfg.get("loudness") or cfg.get("room"))


# ------------------------------------------------------------------ clause planning
@dataclass
class Clause:
    text: str
    pause: float  # seconds of silence after this clause
    sentence_start: bool


_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
_ABBR = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Prof|St|vs|etc|e\.g|i\.e|No|Jr|Sr)\.$", re.I)
_SPLIT = re.compile(r"(\n+|……|\.\.\.|[。！？!?；;：:，,、]|\.(?=\s|$)|——)")


def _kind(p: str) -> str:
    if "\n" in p:
        return "paragraph"
    if p in ("……", "...", "——"):
        return "ellipsis"
    if p in "。！？!?.":
        return "sentence"
    if p in "；;：:":
        return "semi"
    return "comma"


def plan(text: str, cfg: dict, speed: float = 1.0, rng: random.Random | None = None) -> list[Clause]:
    """Split text into clauses with the pause that should follow each one."""
    rng = rng or random.Random()
    parts = _SPLIT.split(text)
    raw: list[tuple[str, str]] = []
    cur = ""
    for piece in parts:
        if not piece:
            continue
        if _SPLIT.fullmatch(piece):
            if piece == "." and _ABBR.search(cur + "."):  # "Mr." is not a sentence end
                cur += piece
                continue
            if piece.strip():
                cur += piece
            raw.append((cur, _kind(piece)))
            cur = ""
        else:
            cur += piece
    if cur.strip():
        raw.append((cur, "sentence"))
    # merge clauses too short to be spoken naturally on their own
    merged: list[list] = []
    for t, k in raw:
        t = t.strip()
        if not t:
            if merged and k == "paragraph":
                merged[-1][1] = "paragraph"
            continue
        short = (len(_CJK.findall(t)) < 4) if _CJK.search(t) else (len(t.split()) < 3)
        # never grow the first clause: it decides how soon the first audio plays
        if len(merged) > 1 and merged[-1][2] and merged[-1][1] in ("comma", "semi"):
            merged[-1][0] += (" " if not _CJK.search(t) else "") + t
            merged[-1][1] = k
            merged[-1][2] = short
        else:
            merged.append([t, k, short])
    base = {"comma": cfg["comma"], "semi": (cfg["comma"] + cfg["sentence"]) / 2, "sentence": cfg["sentence"],
            "ellipsis": max(cfg["sentence"], 0.5), "paragraph": cfg["paragraph"]}
    out = []
    prev = "sentence"
    for t, k, _ in merged:
        j = 1 + rng.uniform(-cfg["jitter"], cfg["jitter"])
        out.append(Clause(t, base[k] * j / max(speed, 0.5), prev in ("sentence", "paragraph", "ellipsis")))
        prev = k
    if out:
        out[-1].pause = 0.0
    return out


# ------------------------------------------------------------------ per-clause trimming
class Trimmer:
    """Drop the engine's leading/trailing silence from one clause while it streams in."""

    HOLD = 0.25  # seconds held back so trailing silence can still be cut

    def __init__(self, sr: int):
        self.sr = sr
        self.started = False
        self.pending = np.zeros(0, np.float32)
        self.peak = 0.0

    def _thr(self) -> float:
        return max(self.peak * 10 ** (-38 / 20), 10 ** (-60 / 20))

    def feed(self, x: np.ndarray) -> np.ndarray:
        self.peak = max(self.peak, float(np.max(np.abs(x))) if len(x) else 0.0)
        self.pending = np.concatenate([self.pending, x])
        if not self.started:
            hit = np.where(np.abs(self.pending) > self._thr())[0]
            if not len(hit):
                return np.zeros(0, np.float32)
            keep = int(self.sr * 0.02)
            self.pending = self.pending[max(0, hit[0] - keep):].copy()
            k = min(len(self.pending), int(self.sr * 0.005))
            self.pending[:k] *= np.linspace(0, 1, k, dtype=np.float32)
            self.started = True
        hold = int(self.sr * self.HOLD)
        if len(self.pending) <= hold:
            return np.zeros(0, np.float32)
        out, self.pending = self.pending[:-hold], self.pending[-hold:]
        return out

    def flush(self) -> np.ndarray:
        x = self.pending
        self.pending = np.zeros(0, np.float32)
        if not len(x):
            return x
        hit = np.where(np.abs(x) > self._thr())[0]
        if not len(hit):
            return np.zeros(0, np.float32)
        x = x[: min(len(x), hit[-1] + int(self.sr * 0.03))].copy()
        k = min(len(x), int(self.sr * 0.008))
        x[-k:] *= np.linspace(1, 0, k, dtype=np.float32)
        return x


# ------------------------------------------------------------------ post chain
_METERS: dict[int, object] = {}


def loudness(x: np.ndarray, sr: int) -> float:
    """Integrated loudness in LUFS (ITU-R BS.1770-4 via pyloudnorm)."""
    import pyloudnorm

    m = _METERS.get(sr) or _METERS.setdefault(sr, pyloudnorm.Meter(sr))
    v = m.integrated_loudness(x.astype(np.float64))
    return float(v) if np.isfinite(v) else -70.0


def _room_ir(sr: int, rt60: float, seed: int = 7) -> np.ndarray:
    """Small-room impulse response: a few early reflections and an exponentially decaying noise tail."""
    rng = np.random.default_rng(seed)
    n = int(sr * rt60 * 1.2)
    t = np.arange(n) / sr
    tail = rng.standard_normal(n) * np.exp(-6.9 * t / rt60)
    tail[: int(sr * 0.012)] = 0  # pre-delay before the diffuse tail
    for ms, g in ((7, 0.5), (11, 0.35), (17, 0.3), (23, 0.22)):
        tail[int(sr * ms / 1000)] += g * 4
    # darker tail: one-pole low-pass, walls absorb highs
    from scipy.signal import lfilter

    tail = lfilter([0.35], [1, -0.65], tail)
    return (tail / np.sqrt(np.sum(tail ** 2))).astype(np.float32)


class Post:
    """Streaming post-processing for one utterance."""

    def __init__(self, sr: int, cfg: dict, seed: int | None = None):
        from scipy.signal import butter

        self.sr, self.cfg = sr, cfg
        self.rng = np.random.default_rng(seed)
        self.gain_db: float | None = None
        self.env = 0.0
        self.ds = butter(2, [5000 / (sr / 2), min(9000, sr / 2 * 0.95) / (sr / 2)], btype="band", output="sos")
        self.ds_zi = None
        self.ir = _room_ir(sr, float(cfg["rt60"])) if cfg.get("room") else None
        self.tail = np.zeros(0, np.float32)
        self.pink = np.zeros(7)
        self.loud_hist: list[np.ndarray] = []

    # --- pieces
    def _compress(self, x: np.ndarray) -> np.ndarray:
        blk = max(1, int(self.sr * 0.005))
        n = len(x) // blk * blk
        if n == 0:
            return x
        rms = np.sqrt(np.mean(x[:n].reshape(-1, blk) ** 2, axis=1) + 1e-12)
        db = 20 * np.log10(rms)
        g = np.zeros_like(db)
        thr, ratio = -24.0, 2.0
        over = db - thr
        g[over > 0] = -over[over > 0] * (1 - 1 / ratio)
        # attack 5 ms / release 80 ms on the gain curve
        out = np.empty_like(g)
        e = self.env
        for i, v in enumerate(g):
            a = 1.0 if v < e else 0.0625
            e = e + a * (v - e)
            out[i] = e
        self.env = e
        lin = np.repeat(10 ** (out / 20), blk)
        lin = np.concatenate([lin, np.full(len(x) - n, lin[-1])])
        return x * lin

    def _deess(self, x: np.ndarray) -> np.ndarray:
        from scipy.signal import sosfilt, sosfilt_zi

        if self.ds_zi is None:
            self.ds_zi = sosfilt_zi(self.ds) * 0
        band, self.ds_zi = sosfilt(self.ds, x, zi=self.ds_zi)
        blk = max(1, int(self.sr * 0.005))
        n = len(x) // blk * blk
        if n == 0:
            return x
        eb = np.sqrt(np.mean(band[:n].reshape(-1, blk) ** 2, 1) + 1e-12)
        ex = np.sqrt(np.mean(x[:n].reshape(-1, blk) ** 2, 1) + 1e-12)
        ratio_db = 20 * np.log10(eb / ex)
        cut = np.clip((ratio_db + 8) / 6, 0, 1) * 0.5  # up to -6 dB on the band when sibilance dominates
        cut = np.repeat(cut, blk)
        cut = np.concatenate([cut, np.full(len(x) - n, cut[-1])])
        return x - band * cut

    def _loud(self, x: np.ndarray) -> np.ndarray:
        self.loud_hist.append(x)
        hist = np.concatenate(self.loud_hist)[-int(self.sr * 20):]
        if len(hist) >= int(self.sr * 0.4):
            target = float(self.cfg["target_lufs"]) - loudness(hist, self.sr)
            target = float(np.clip(target, -12, 12))
            self.gain_db = target if self.gain_db is None else self.gain_db + 0.3 * (target - self.gain_db)
        g = 10 ** ((self.gain_db or 0.0) / 20)
        return x * g

    def _reverb(self, x: np.ndarray) -> np.ndarray:
        n = len(x) + len(self.ir) - 1
        nfft = 1 << (n - 1).bit_length()
        wet = np.fft.irfft(np.fft.rfft(x, nfft) * np.fft.rfft(self.ir, nfft), nfft)[:n].astype(np.float32)
        if len(self.tail):
            m = min(len(self.tail), len(wet))
            wet[:m] += self.tail[:m]
            if len(self.tail) > m:
                wet = np.concatenate([wet, self.tail[m:]])
        self.tail = wet[len(x):].copy()
        return x + wet[: len(x)] * 10 ** (float(self.cfg["reverb_wet_db"]) / 20)

    def _tone(self, n: int) -> np.ndarray:
        # Paul Kellet's economy pink-noise filter on white noise
        w = self.rng.standard_normal(n).astype(np.float64)
        from scipy.signal import lfilter

        b = [0.049922035, -0.095993537, 0.050612699, -0.004408786]
        a = [1, -2.494956002, 2.017265875, -0.522189400]
        zi = self.pink[:3]
        y, zf = lfilter(b, a, w, zi=zi)
        self.pink[:3] = zf
        y /= 0.08  # ~unit RMS
        return (y * 10 ** (float(self.cfg["room_tone_db"]) / 20)).astype(np.float32)

    # --- public
    def process(self, x: np.ndarray, silent: bool = False) -> np.ndarray:
        """x: float32 clause audio (or silence for pauses when silent=True)."""
        y = x.astype(np.float32)
        if not silent:
            if self.cfg.get("dynamics"):
                y = self._deess(self._compress(y))
            if self.cfg.get("loudness"):
                y = self._loud(y)
        elif self.cfg.get("loudness") and self.gain_db is not None:
            y = y * 10 ** (self.gain_db / 20)
        if self.ir is not None:
            y = self._reverb(y)
            y = y + self._tone(len(y))
        peak = 10 ** (-1 / 20)
        over = np.abs(y) > peak * 0.8
        if over.any():  # soft knee limiter
            s = np.sign(y[over])
            a = np.abs(y[over]) - peak * 0.8
            y[over] = s * (peak * 0.8 + a / (1 + a / (peak * 0.2)))
        return y.astype(np.float32)

    def tail_out(self) -> np.ndarray:
        """Let the reverb ring out at the end of the utterance."""
        if self.ir is None or not len(self.tail):
            return np.zeros(0, np.float32)
        t = self.tail * 10 ** (float(self.cfg["reverb_wet_db"]) / 20)
        t = t + self._tone(len(t))
        t *= np.linspace(1, 0, len(t), dtype=np.float32)
        self.tail = np.zeros(0, np.float32)
        return t.astype(np.float32)


def prewarm() -> None:
    """Import scipy / pyloudnorm and run the chain once, so the first spoken clause pays no start-up cost."""
    x = (np.random.default_rng(0).standard_normal(32000) * 0.05).astype(np.float32)
    Post(32000, DEFAULTS).process(x)
