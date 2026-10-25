"""Breath library: find inhalations in a voice's own material and keep them for synthesis.

An inhalation is an unvoiced (no F0), noise-like stretch of 150-500 ms whose level sits well below
the speech around it (about 12-40 dB under the 95th-percentile frame level), that ends shortly
before speech starts. Fricatives are shorter and brighter, so duration and spectral centroid
separate them. Clips are stored with their level relative to the source speech, so when they are
inserted later they keep the speaker's natural breath-to-speech ratio.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import soundfile as sf

from . import audio_util, voices

SR = 16000
OUT_SR = 32000
MIN_S, MAX_S = 0.15, 0.5
FLAT_MIN = 0.01


def _frames(x: np.ndarray, n: int, hop: int) -> np.ndarray:
    if len(x) < n:
        return np.zeros((0, n), np.float32)
    idx = np.arange(n)[None, :] + hop * np.arange((len(x) - n) // hop + 1)[:, None]
    return x[idx]


def detect(x16: np.ndarray) -> list[tuple[float, float, float]]:
    """(start s, end s, level dB relative to speech) for each inhalation found in 16 kHz audio."""
    import pyworld as pw

    if len(x16) < SR:
        return []
    hop, n = 160, 320
    fr = _frames(x16, n, hop) * np.hanning(n)
    rms = 10 * np.log10(np.mean(fr ** 2, axis=1) + 1e-12)
    spec = np.abs(np.fft.rfft(fr, axis=1)) ** 2
    freqs = np.fft.rfftfreq(n, 1 / SR)
    centroid = (spec * freqs).sum(1) / (spec.sum(1) + 1e-12)
    flat = np.exp(np.mean(np.log(spec[:, 4:] + 1e-12), 1)) / (np.mean(spec[:, 4:], 1) + 1e-12)
    xd = x16.astype(np.float64)
    f0, t = pw.dio(xd, SR, frame_period=10.0)  # default (strict) voicing: breaths must not count as voiced
    f0 = pw.stonemask(xd, f0, t, SR)
    f0 = np.interp(np.arange(len(rms)), np.arange(len(f0)), f0)
    voiced = f0 > 0
    speech = np.percentile(rms, 95)
    cand = (~voiced) & (rms > speech - 40) & (rms < speech - 12) & (centroid > 600) & (centroid < 4500)
    out = []
    i = 0
    while i < len(cand):
        if not cand[i]:
            i += 1
            continue
        j = i
        while j < len(cand) and (cand[j] or (j + 1 < len(cand) and cand[j + 1])):  # bridge 1-frame gaps
            j += 1
        dur = (j - i) * hop / SR
        before_quiet = i < 5 or np.mean(rms[max(0, i - 8):i]) < speech - 30 or np.mean(rms[max(0, i - 8):i]) < rms[i:j].mean() + 3
        after = voiced[j: j + 60]  # speech should follow within 0.6 s
        if MIN_S <= dur <= MAX_S and before_quiet and after.any() and np.mean(flat[i:j]) > FLAT_MIN:
            out.append((i * hop / SR, j * hop / SR, float(np.mean(rms[i:j]) - speech)))
        i = j + 1
    return out


def lib_dir(vid: str) -> Path:
    return voices.folder(vid) / "breaths"


def build(vid: str, files: list[Path], limit: int = 40) -> list[dict]:
    """Scan material files, save up to `limit` breath clips, return their index."""
    d = lib_dir(vid)
    d.mkdir(parents=True, exist_ok=True)
    for p in d.glob("*.wav"):
        p.unlink()
    found = []
    for f in files:
        try:
            x16 = audio_util.decode(f, SR)
            hi = audio_util.decode(f, OUT_SR)
        except Exception:
            continue
        for a, b, lvl in detect(x16):
            seg = hi[int(a * OUT_SR): int(b * OUT_SR)].copy()
            k = min(len(seg) // 4, int(OUT_SR * 0.03))
            if k:
                ramp = np.linspace(0, 1, k, dtype=np.float32)
                seg[:k] *= ramp
                seg[-k:] *= ramp[::-1]
            found.append((lvl, seg, f.name, a))
    # prefer clips at typical levels (-30..-18 dB under speech) and drop near-duplicates by position
    found.sort(key=lambda r: abs(r[0] + 24))
    index = []
    for lvl, seg, name, a in found[:limit]:
        fn = f"b{len(index):02d}.wav"
        peak = float(np.max(np.abs(seg)) + 1e-9)
        sf.write(d / fn, seg / peak * 0.5, OUT_SR, subtype="PCM_16")  # stored normalised, level kept in index
        index.append({"file": fn, "seconds": round(len(seg) / OUT_SR, 3), "rel_db": round(lvl, 1), "source": name, "at": round(a, 2)})
    (d / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    return index


def load(vid: str) -> list[dict]:
    p = lib_dir(vid) / "index.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
