"""Audio decoding/encoding helpers built on ffmpeg and soundfile."""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from . import config
from .procutil import CREATE_FLAGS


def ffmpeg_exe() -> str:
    bundled = config.resolve(config.load_settings()["engine"]["dir"]) / "runtime" / "ffmpeg.exe"
    if bundled.exists():
        return str(bundled)
    found = shutil.which("ffmpeg")
    if not found:
        raise RuntimeError("没有找到 ffmpeg，请安装 ffmpeg 或先安装 GPT-SoVITS 引擎（自带 ffmpeg）")
    return found


def decode(src: str | Path | bytes, sr: int = 16000) -> np.ndarray:
    """Decode any audio/video file or bytes to mono float32 at `sr`."""
    if isinstance(src, (str, Path)):
        cmd = [ffmpeg_exe(), "-nostdin", "-v", "error", "-i", str(src)]
        data = None
    else:
        cmd = [ffmpeg_exe(), "-nostdin", "-v", "error", "-i", "pipe:0"]
        data = src
    cmd += ["-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"]
    p = subprocess.run(cmd, input=data, capture_output=True, creationflags=CREATE_FLAGS)
    if p.returncode != 0:
        raise ValueError("无法解码音频：" + p.stderr.decode("utf-8", "replace").strip()[-300:])
    return np.frombuffer(p.stdout, dtype=np.float32).copy()


def to_wav_file(src: str | Path | bytes, dst: Path, sr: int = 32000) -> float:
    """Convert to 16-bit mono WAV at `sr`; return duration in seconds."""
    x = decode(src, sr)
    dst.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dst), x, sr, subtype="PCM_16")
    return len(x) / sr


def encode(pcm16: np.ndarray, sr: int, fmt: str) -> tuple[bytes, str]:
    """Encode int16 mono PCM into an OpenAI-style response_format. Returns (bytes, mime)."""
    fmt = (fmt or "wav").lower()
    if fmt == "pcm":
        return pcm16.astype("<i2").tobytes(), "audio/pcm"
    if fmt in ("wav", "flac"):
        buf = io.BytesIO()
        sf.write(buf, pcm16, sr, format=fmt.upper(), subtype="PCM_16")
        return buf.getvalue(), f"audio/{fmt}"
    codecs = {
        "mp3": (["-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3"], "audio/mpeg"),
        "opus": (["-c:a", "libopus", "-b:a", "64k", "-f", "ogg"], "audio/ogg"),
        "aac": (["-c:a", "aac", "-b:a", "128k", "-f", "adts"], "audio/aac"),
    }
    if fmt not in codecs:
        raise ValueError(f"不支持的 response_format：{fmt}（可用 wav, mp3, opus, aac, flac, pcm）")
    args, mime = codecs[fmt]
    cmd = [ffmpeg_exe(), "-nostdin", "-v", "error", "-f", "s16le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0", *args, "pipe:1"]
    p = subprocess.run(cmd, input=pcm16.astype("<i2").tobytes(), capture_output=True, creationflags=CREATE_FLAGS)
    if p.returncode != 0:
        raise ValueError("音频编码失败：" + p.stderr.decode("utf-8", "replace")[-300:])
    return p.stdout, mime
