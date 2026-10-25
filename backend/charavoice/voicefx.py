"""Voice tweaks applied after synthesis: pitch, formant (age / gender colour), brightness, breathiness.

Chosen after a bake-off (see Docs/Implementation.html):
- pitch: TD-PSOLA on DIO pitch marks (pyworld). It keeps the spectral envelope, so the voice gets
  higher or lower without changing timbre, and it is ~12 ms per second of audio. Phase vocoders
  smear transients and WORLD resynthesis adds a buzzy colouration even with no change at all.
- formant / brightness / breath: one STFT pass that keeps the original phase. The spectral envelope
  is taken from the low quefrencies of the log spectrum (cepstral liftering); the fine structure is
  divided out, the envelope is warped along frequency (formant), tilted (brightness), and then the
  fine structure is put back. Breathiness adds envelope-shaped noise above ~1.5 kHz.
Speed is not done here: the engines change speaking rate natively, which sounds far better.

`Stream` processes audio incrementally with a short look-ahead so tweaks also work while streaming.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

LIMITS = {"pitch": (-6.0, 6.0), "formant": (-20.0, 20.0), "brightness": (-6.0, 6.0), "breath": (0.0, 1.0)}

PRESETS: dict[str, dict[str, float]] = {
    "sweet": {"pitch": 2.0, "formant": 8.0, "brightness": 2.0, "breath": 0.15},
    "deep": {"pitch": -3.0, "formant": -8.0, "brightness": -2.0, "breath": 0.0},
    "younger": {"pitch": 1.5, "formant": 12.0, "brightness": 1.5, "breath": 0.0},
    "mature": {"pitch": -1.5, "formant": -10.0, "brightness": -1.0, "breath": 0.1},
}
PRESET_LABEL = {"sweet": "更甜", "deep": "更低沉", "younger": "更年轻", "mature": "更成熟"}


@dataclass
class Tweak:
    pitch: float = 0.0  # semitones
    formant: float = 0.0  # percent; + = smaller vocal tract (younger / more feminine)
    brightness: float = 0.0  # dB at 8 kHz relative to 1 kHz
    breath: float = 0.0  # 0..1

    @classmethod
    def of(cls, d: dict | None) -> "Tweak":
        d = d or {}
        vals = {}
        for k, (lo, hi) in LIMITS.items():
            try:
                vals[k] = float(min(hi, max(lo, float(d.get(k) or 0.0))))
            except (TypeError, ValueError):
                raise ValueError(f"声线参数 {k} 必须是数字") from None
        return cls(**vals)

    def merged(self, override: dict | None) -> "Tweak":
        d = asdict(self)
        d.update({k: v for k, v in (override or {}).items() if v is not None and k in LIMITS})
        return Tweak.of(d)

    @property
    def active(self) -> bool:
        return any(abs(v) > 1e-3 for v in asdict(self).values())

    def dict(self) -> dict[str, float]:
        return {k: round(v, 3) for k, v in asdict(self).items()}


def resolve(base: dict | None, preset: str | None = None, override: dict | None = None) -> Tweak:
    """Voice defaults, then a preset (replaces them), then explicit per-request values."""
    t = Tweak.of(base)
    if preset:
        if preset not in PRESETS:
            raise ValueError(f"未知的声线预设：{preset}（可用：{', '.join(PRESETS)}）")
        t = Tweak.of(PRESETS[preset])
    return t.merged(override)


# ------------------------------------------------------------------ pitch (TD-PSOLA)
def estimate_f0(x: np.ndarray, sr: int, frame_period: float = 5.0) -> np.ndarray:
    """DIO + StoneMask on a ~16 kHz copy. allowed_range=0.2 keeps far more voiced frames than the
    default; short unvoiced gaps inside speech are bridged so they get shifted too."""
    import pyworld as pw

    q = max(1, sr // 16000)
    xd = x.astype(np.float64)
    if q > 1:
        n = len(xd) // q * q
        xd = xd[:n].reshape(-1, q).mean(1)
    fs = sr / q
    xd = np.ascontiguousarray(xd)
    f0, t = pw.dio(xd, fs, frame_period=frame_period, f0_floor=60, f0_ceil=700, allowed_range=0.2)
    f0 = pw.stonemask(xd, f0, t, fs)
    v = f0 > 0
    gap = int(40 / frame_period)
    i = 0
    while i < len(f0):
        if v[i]:
            i += 1
            continue
        j = i
        while j < len(f0) and not v[j]:
            j += 1
        if 0 < i and j < len(f0) and j - i <= gap:
            f0[i:j] = np.linspace(f0[i - 1], f0[j], j - i + 2)[1:-1]
        i = j
    return f0


def _marks(x: np.ndarray, sr: int, f0: np.ndarray, hop: int) -> tuple[np.ndarray, np.ndarray]:
    """Analysis marks one period apart in voiced frames, every 5 ms elsewhere. Snapping marks to
    waveform peaks was tried and lowered predicted MOS by ~0.4 at +3 st, so marks follow F0 only."""
    marks, voiced = [], []
    t, n = 0.0, len(x)
    while t < n:
        f = f0[min(int(t / hop), len(f0) - 1)]
        marks.append(int(t))
        voiced.append(bool(f > 0))
        t += sr / f if f > 0 else sr * 0.005
    return np.array(marks, dtype=np.int64), np.array(voiced)


def psola(x: np.ndarray, sr: int, semitones: float) -> np.ndarray:
    if abs(semitones) < 1e-3 or len(x) < sr // 50:
        return x
    frame_period = 5.0
    hop = int(sr * frame_period / 1000)
    f0 = estimate_f0(x, sr, frame_period)
    r = 2 ** (semitones / 12)
    am, av = _marks(x, sr, f0, hop)
    n = len(x)
    y = np.zeros(n + sr // 10, dtype=np.float64)
    wsum = np.zeros_like(y)
    ts = float(am[0]) if len(am) else 0.0
    while ts < n:
        k = min(int(np.searchsorted(am, ts)), len(am) - 1)
        if k > 0 and abs(am[k - 1] - ts) < abs(am[k] - ts):
            k -= 1
        c = int(am[k])
        if av[k]:
            T = sr / max(f0[min(c // hop, len(f0) - 1)], 1.0)
            step = T / r
        else:
            T = step = sr * 0.005
        L = max(2, int(T))
        g = np.zeros(2 * L)
        a, b = max(c - L, 0), min(c + L, n)
        g[a - (c - L): b - (c - L)] = x[a:b]
        w = np.hanning(2 * L)
        pos = int(ts) - L
        pa, pb = max(pos, 0), min(pos + 2 * L, len(y))
        if pb > pa:
            y[pa:pb] += (g * w)[pa - pos: pb - pos]
            wsum[pa:pb] += w[pa - pos: pb - pos]
        ts += step
    # normalise by the window sum smoothed over ~20 ms; a per-sample division would flatten the new
    # pulse train and pull the perceived pitch back towards the original
    k = max(1, int(sr * 0.02))
    smooth = np.convolve(wsum[:n], np.ones(k) / k, mode="same")
    return (y[:n] / np.maximum(smooth, 0.25)).astype(np.float32)


# ------------------------------------------------------------------ envelope pass (STFT)
def _frame(sr: int) -> tuple[int, int]:
    n = 1 << int(np.ceil(np.log2(sr * 0.032)))  # ~32 ms
    return n, n // 4


def envelope_pass(x: np.ndarray, sr: int, t: Tweak, seed: int = 0) -> np.ndarray:
    if abs(t.formant) < 1e-3 and abs(t.brightness) < 1e-3 and t.breath < 1e-3:
        return x
    N, H = _frame(sr)
    pad = N
    xp = np.concatenate([np.zeros(pad, np.float32), x.astype(np.float32), np.zeros(pad + N, np.float32)])
    win = np.hanning(N + 1)[:-1].astype(np.float32)
    nfr = (len(xp) - N) // H + 1
    idx = np.arange(N)[None, :] + H * np.arange(nfr)[:, None]
    X = np.fft.rfft(xp[idx] * win, axis=1)
    mag = np.abs(X) + 1e-9
    nb = mag.shape[1]
    freqs = np.arange(nb) * sr / N
    # spectral envelope by cepstral liftering (low quefrencies of the log magnitude)
    q = max(4, int(sr * 0.0015))
    cep = np.fft.irfft(np.log(mag), axis=1)
    cep[:, q:-q] = 0
    env = np.exp(np.fft.rfft(cep, axis=1).real)
    gain = np.ones_like(mag)
    if abs(t.formant) > 1e-3:
        a = 1 + t.formant / 100  # alpha > 1 moves the formants up
        src = np.arange(nb) / a
        warped = np.stack([np.interp(src, np.arange(nb), row) for row in env])
        gain *= warped / env
    if abs(t.brightness) > 1e-3:
        octaves = np.log2(np.maximum(freqs, 125.0) / 1000.0) / 3.0  # 1 kHz -> 0, 8 kHz -> 1
        gain *= (10 ** (t.brightness * np.clip(octaves, -1, 1) / 20))[None, :]
    Y = X * gain
    if t.breath > 1e-3:
        rng = np.random.default_rng(seed)
        hi = np.clip((freqs - 1500) / 1500, 0, 1)[None, :]
        low = mag[:, (freqs > 80) & (freqs < 1000)].mean(1, keepdims=True)
        voiced = np.clip(low / (np.percentile(low, 90) + 1e-9) * 2 - 0.2, 0, 1)  # soft gate on speech frames
        noise = np.exp(2j * np.pi * rng.random(Y.shape))
        Y = Y + t.breath * 1.2 * env * gain * hi * voiced * noise
    y = np.fft.irfft(Y, n=N, axis=1) * win
    out = np.zeros(len(xp), np.float32)
    norm = np.zeros(len(xp), np.float32)
    for i in range(nfr):
        out[i * H: i * H + N] += y[i]
        norm[i * H: i * H + N] += win ** 2
    out = out / np.maximum(norm, 1e-6)
    return out[pad: pad + len(x)].astype(np.float32)


def process(x: np.ndarray, sr: int, t: Tweak, seed: int = 0) -> np.ndarray:
    """float32 mono in [-1, 1] -> tweaked float32 of the same length."""
    if not t.active or len(x) == 0:
        return x
    y = psola(x, sr, t.pitch)
    y = envelope_pass(y, sr, t, seed)
    return np.clip(y, -1.0, 1.0)


class Stream:
    """Incremental processing with LOOKAHEAD seconds of right context and CONTEXT of left context;
    consecutive blocks are joined with a short crossfade so there are no clicks."""

    CONTEXT, LOOKAHEAD, XFADE, BLOCK = 0.08, 0.04, 0.01, 1.0

    def __init__(self, sr: int, tweak: Tweak):
        self.sr, self.t = sr, tweak
        self.buf = np.zeros(0, np.float32)  # input not yet emitted, plus left context in front
        self.left = 0  # samples of left context at the start of buf
        self.tail = np.zeros(0, np.float32)  # processed samples just past the last emitted one
        self.seed = 0

    def feed(self, x: np.ndarray):
        """Push float32 samples; yields processed blocks (<= BLOCK s) as soon as each is ready."""
        self.buf = np.concatenate([self.buf, x.astype(np.float32)])
        la = int(self.sr * self.LOOKAHEAD)
        blk = int(self.sr * self.BLOCK)
        while len(self.buf) - self.left - la > 0:
            emit = min(blk, len(self.buf) - self.left - la)
            yield self._run(emit, final=False)

    def flush(self) -> np.ndarray:
        out = self._run(len(self.buf) - self.left, final=True) if len(self.buf) > self.left else np.zeros(0, np.float32)
        self.buf, self.left, self.tail = np.zeros(0, np.float32), 0, np.zeros(0, np.float32)
        return out

    def _run(self, emit: int, final: bool) -> np.ndarray:
        sr = self.sr
        end = len(self.buf) if final else min(len(self.buf), self.left + emit + int(sr * self.LOOKAHEAD))
        seg = self.buf[:end]
        self.seed += 1
        y = process(seg, sr, self.t, self.seed)
        out = y[self.left: self.left + emit].copy()
        xf = min(len(self.tail), len(out), int(sr * self.XFADE))
        if xf:
            ramp = np.linspace(0, 1, xf, dtype=np.float32)
            out[:xf] = self.tail[:xf] * (1 - ramp) + out[:xf] * ramp
        self.tail = y[self.left + emit: self.left + emit + int(sr * self.XFADE)].copy()
        keep = int(sr * self.CONTEXT)
        consumed = self.left + emit
        start = max(0, consumed - keep)
        self.buf = self.buf[start:]
        self.left = consumed - start
        return out
