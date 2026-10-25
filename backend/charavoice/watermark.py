"""AudioSeal (MIT, code and weights) imperceptible watermark for generated speech.

The generator is applied at 16 kHz (the rate the models are trained for); the watermark
signal is then resampled back to the output rate and added. Models are cached under
models/audioseal. A 16-bit message tags the audio as CharaVoice output.
"""

from __future__ import annotations

import os
import threading

import numpy as np

from . import config

os.environ.setdefault("AUDIOSEAL_CACHE_DIR", str(config.MODELS_DIR / "audioseal"))
# AudioSeal 0.2 wraps its encoder in torch.compile, which needs a C++ compiler on Windows;
# its moshi utils honour NO_TORCH_COMPILE, and eager mode is fast enough on CPU here.
os.environ.setdefault("NO_TORCH_COMPILE", "1")
GEN_CARD = "audioseal_wm_16bits"
DET_CARD = "audioseal_detector_16bits"
# "CV" in ASCII folded into 16 bits: identifies CharaVoice-generated audio
MESSAGE_BITS = [int(b) for b in format((ord("C") << 8) | ord("V"), "016b")]
_lock = threading.Lock()
_gen = None
_det = None


def _resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out:
        return x
    n = int(round(len(x) * sr_out / sr_in))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def _models(detector: bool = False):
    global _gen, _det
    import torch
    from audioseal import AudioSeal

    with _lock:
        torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
        if _gen is None:
            _gen = AudioSeal.load_generator(GEN_CARD).eval()
        if detector and _det is None:
            _det = AudioSeal.load_detector(DET_CARD).eval()
    return _gen, _det


def embed(pcm16: np.ndarray, sr: int) -> np.ndarray:
    import torch

    gen, _ = _models()
    x = pcm16.astype(np.float32) / 32768.0
    x16 = _resample(x, sr, 16000)
    with torch.no_grad():
        wav = torch.from_numpy(x16).view(1, 1, -1)
        msg = torch.tensor([MESSAGE_BITS], dtype=torch.int32)
        wm = gen.get_watermark(wav, 16000, message=msg).view(-1).numpy()
    w = _resample(wm, 16000, sr)[: len(x)]
    if len(w) < len(x):  # resampling odd lengths can come back one sample short
        w = np.pad(w, (0, len(x) - len(w)))
    out = x + w
    return (np.clip(out, -1, 1) * 32767).astype(np.int16)


def detect(samples: np.ndarray, sr: int) -> dict:
    """Probability that the clip carries an AudioSeal watermark, and whether the message is ours."""
    import torch

    _, det = _models(detector=True)
    x16 = _resample(samples.astype(np.float32), sr, 16000)
    with torch.no_grad():
        prob, msg = det.detect_watermark(torch.from_numpy(x16).view(1, 1, -1), 16000)
    bits = [int(round(float(b))) for b in msg.view(-1).tolist()]
    return {"probability": round(float(prob), 3), "charavoice": bits == MESSAGE_BITS and float(prob) > 0.5}
