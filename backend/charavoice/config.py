"""Paths, user settings and the runtime file that clients read to find the backend.

Every path is derived from the install root (the repository folder by default),
so nothing machine-specific is ever written into source or committed config.
Override the root with the CHARAVOICE_HOME environment variable.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("CHARAVOICE_HOME") or Path(__file__).resolve().parents[2])
DATA_DIR = ROOT / "data"
VOICES_DIR = DATA_DIR / "voices"
LOG_DIR = DATA_DIR / "logs"
CACHE_DIR = DATA_DIR / "cache"
ENGINE_DATA_DIR = DATA_DIR / "engine"
SETTINGS_FILE = DATA_DIR / "settings.json"
RUNTIME_FILE = DATA_DIR / "runtime.json"
MODELS_DIR = ROOT / "models"
ENGINES_DIR = ROOT / "engines"

DEFAULT_SETTINGS: dict[str, Any] = {
    "server": {"host": "127.0.0.1", "port": 9870},
    "engine": {
        "dir": "engines/GPT-SoVITS",
        "port": 9880,
        "autostart": True,
        "device": "cuda",
        "is_half": True,
        # Base model loaded at engine start-up and used as the training base.
        "version": "v2Pro",
        # Base model for new zero-shot voices: V5 turbo clones markedly closer without training
        # (measured speaker similarity 0.78 vs 0.48 for v2Pro on the same reference).
        "zero_shot_base": "v5turbo",
        "start_timeout": 120,
        # 0 = whole utterance, 1 = per sentence (best quality), 2/3 = token streaming.
        "streaming_mode": 1,
        # CUDA Graph speeds up the autoregressive stage once captured; capture costs time on first use.
        "use_cuda_graph": True,
        # Synthesize a short sentence right after start-up so the first real call is fast.
        "warmup": True,
        # Seconds without audio from the engine before the request fails and the engine is restarted.
        "synth_timeout": 90,
    },
    "speech": {
        "default_voice": "",
        "play_locally": True,
        "output_device": None,
    },
    "listen": {
        "engine": "sensevoice",  # "sensevoice" | "whisper"
        "input_device": None,
        "vad_threshold": 0.5,
        "min_silence": 0.5,
        # A conversational turn ends after this much silence (one-shot listen joins segments).
        "turn_silence": 1.2,
        "min_speech": 0.25,
        "max_speech": 20.0,
        "pause_while_speaking": True,
        "num_threads": 2,
        "sensevoice_dir": "models/asr/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "sensevoice_model": "model.int8.onnx",
        "whisper_dir": "models/asr/sherpa-onnx-whisper-small",
        "whisper_prefix": "small",
        "vad_model": "models/asr/silero_vad.onnx",
    },
    "compliance": {"metadata_tag": True, "watermark": True},
}

_lock = threading.Lock()


def _merge(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def ensure_dirs() -> None:
    for d in (DATA_DIR, VOICES_DIR, LOG_DIR, CACHE_DIR, ENGINE_DATA_DIR):
        d.mkdir(parents=True, exist_ok=True)


def resolve(p: str | os.PathLike) -> Path:
    """Resolve a settings path: relative values are relative to ROOT."""
    path = Path(p)
    return path if path.is_absolute() else ROOT / path


def load_settings() -> dict[str, Any]:
    with _lock:
        if SETTINGS_FILE.exists():
            try:
                user = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                user = {}
        else:
            user = {}
        return _merge(DEFAULT_SETTINGS, user)


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a partial settings dict into settings.json and return the full result."""
    with _lock:
        current = {}
        if SETTINGS_FILE.exists():
            try:
                current = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                current = {}
        merged = _merge(current, patch)
        ensure_dirs()
        SETTINGS_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_settings()


def write_runtime(port: int, pid: int) -> dict[str, Any]:
    """Generate a fresh access token and publish how to reach this backend."""
    info = {
        "base_url": f"http://127.0.0.1:{port}",
        "port": port,
        "token": secrets.token_urlsafe(24),
        "pid": pid,
    }
    ensure_dirs()
    tmp = RUNTIME_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(info, indent=2), encoding="utf-8")
    os.replace(tmp, RUNTIME_FILE)
    return info


def read_runtime() -> dict[str, Any] | None:
    try:
        return json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def clear_runtime(pid: int) -> None:
    info = read_runtime()
    if info and info.get("pid") == pid:
        try:
            RUNTIME_FILE.unlink()
        except FileNotFoundError:
            pass
