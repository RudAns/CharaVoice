"""Marking generated audio as AI-generated.

* Metadata: WAV/FLAC get INFO/Vorbis tags via libsndfile, compressed formats get
  ffmpeg -metadata tags, and every HTTP response carries X-CharaVoice-AI-Generated.
* Watermark: optional AudioSeal embedding (see watermark.py), on by default.
"""

from __future__ import annotations

import io
import struct
import subprocess

import numpy as np
import soundfile as sf

from . import APP_NAME, __version__, audio_util, config
from .procutil import CREATE_FLAGS

AI_COMMENT = f"AI-generated speech (synthetic voice) - {APP_NAME} {__version__}"


def wav_stream_header(sr: int) -> bytes:
    """WAV header with unknown length (0xFFFFFFFF), for chunked streaming."""
    return (
        b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
        + b"data" + struct.pack("<I", 0xFFFFFFFF)
    )


def finalize(pcm16: np.ndarray, sr: int) -> np.ndarray:
    """Post-process a complete utterance before it leaves the API (watermark when enabled)."""
    if config.load_settings()["compliance"]["watermark"]:
        try:
            from . import watermark

            return watermark.embed(pcm16, sr)
        except ImportError:
            return pcm16
    return pcm16


def encode_tagged(pcm16: np.ndarray, sr: int, fmt: str, voice: dict) -> tuple[bytes, str]:
    fmt = (fmt or "wav").lower()
    tag = config.load_settings()["compliance"]["metadata_tag"]
    title = f"{voice.get('name', '')} ({APP_NAME} synthetic voice)"
    if fmt in ("wav", "flac"):
        buf = io.BytesIO()
        with sf.SoundFile(buf, "w", samplerate=sr, channels=1, format=fmt.upper(), subtype="PCM_16") as f:
            if tag:
                f.title = title
                f.comment = AI_COMMENT
                f.software = f"{APP_NAME} {__version__}"
            f.write(pcm16)
        return buf.getvalue(), f"audio/{fmt}"
    if fmt == "pcm" or not tag:
        return audio_util.encode(pcm16, sr, fmt)
    codecs = {
        "mp3": (["-c:a", "libmp3lame", "-b:a", "128k", "-id3v2_version", "3", "-f", "mp3"], "audio/mpeg"),
        "opus": (["-c:a", "libopus", "-b:a", "64k", "-f", "ogg"], "audio/ogg"),
        "aac": (["-c:a", "aac", "-b:a", "128k", "-f", "adts"], "audio/aac"),
    }
    if fmt not in codecs:
        raise ValueError(f"不支持的 response_format：{fmt}（可用 wav, mp3, opus, aac, flac, pcm）")
    args, mime = codecs[fmt]
    meta = ["-metadata", f"title={title}", "-metadata", f"comment={AI_COMMENT}", "-metadata", f"encoder={APP_NAME}"]
    cmd = [audio_util.ffmpeg_exe(), "-nostdin", "-v", "error", "-f", "s16le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0", *meta, *args, "pipe:1"]
    p = subprocess.run(cmd, input=pcm16.astype("<i2").tobytes(), capture_output=True, creationflags=CREATE_FLAGS)
    if p.returncode != 0:
        raise ValueError("音频编码失败：" + p.stderr.decode("utf-8", "replace")[-300:])
    return p.stdout, mime
