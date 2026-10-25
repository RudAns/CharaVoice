"""Voice projects: one folder per voice under data/voices/<id>/.

    voice.json        metadata (name, reference clip, weights, consent, avatars)
    ref/              reference clip(s) for zero-shot / prompt conditioning
    avatar/           character images or short loops per presence state
    weights/          fine-tuned GPT (.ckpt) and SoVITS (.pth) weights
    dataset/          prepared training material (M3)

Paths inside voice.json are relative to the voice folder so the folder can be
moved or shared as a unit.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from .events import bus

CONSENT_NOTICE = "请告知被模仿的人并取得其单独同意。"
# Emotion labels for extra reference clips (same set SenseVoice reports, plus "gentle").
EMOTIONS = {
    "neutral": "平静", "happy": "开心", "angry": "生气", "sad": "悲伤",
    "surprised": "惊讶", "fearful": "害怕", "gentle": "温柔",
}
CONSENT_KINDS = {"self": "本人声音", "authorized": "已获授权的声音"}
AVATAR_STATES = ("default", "idle", "listening", "speaking")
AVATAR_EXT = {".gif", ".png", ".apng", ".webp", ".webm", ".mp4"}
LANGS = ("zh", "en", "ja")
_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24]
    return f"{s}-{uuid.uuid4().hex[:4]}" if s else f"voice-{uuid.uuid4().hex[:6]}"


def folder(vid: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", vid or ""):
        raise KeyError(vid)
    return config.VOICES_DIR / vid


def _read(vid: str) -> dict[str, Any]:
    p = folder(vid) / "voice.json"
    if not p.exists():
        raise KeyError(vid)
    return json.loads(p.read_text(encoding="utf-8"))


def _write(v: dict[str, Any]) -> None:
    d = folder(v["id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "voice.json.tmp"
    tmp.write_text(json.dumps(v, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(d / "voice.json")
    bus.publish("voices_changed", id=v["id"])


def get(vid: str) -> dict[str, Any]:
    return _read(vid)


def list_all() -> list[dict[str, Any]]:
    config.ensure_dirs()
    out = []
    for d in sorted(config.VOICES_DIR.iterdir()):
        if (d / "voice.json").exists():
            try:
                out.append(json.loads((d / "voice.json").read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                continue
    out.sort(key=lambda v: v.get("created_at", ""))
    return out


def find(name_or_id: str | None) -> dict[str, Any]:
    """Resolve a voice by id or display name; fall back to the default voice."""
    voices = list_all()
    if not voices:
        raise LookupError("还没有任何声音。请先在 CharaVoice 里新建一个声音。")
    if name_or_id:
        for v in voices:
            if v["id"] == name_or_id or v["name"] == name_or_id:
                return v
        raise LookupError(f"找不到声音“{name_or_id}”。可用：{', '.join(v['id'] for v in voices)}")
    default = config.load_settings()["speech"]["default_voice"]
    for v in voices:
        if v["id"] == default:
            return v
    return voices[0]


def create(name: str, consent_kind: str, language: str = "zh", base: str | None = None) -> dict[str, Any]:
    if consent_kind not in CONSENT_KINDS:
        raise ValueError("新建声音前必须选择“本人声音”或“已获授权的声音”。")
    name = (name or "").strip()
    if not name:
        raise ValueError("请填写声音名称。")
    with _lock:
        vid = _slug(name)
        v = {
            "id": vid,
            "name": name,
            "created_at": now_iso(),
            "language": language if language in LANGS else "zh",
            "base": base or config.load_settings()["engine"].get("zero_shot_base", "v5turbo"),
            "ref": None,
            "aux_refs": [],
            "weights": {"gpt": None, "sovits": None, "trained_at": None},
            "consent": {
                "kind": consent_kind,
                "label": CONSENT_KINDS[consent_kind],
                "notice": CONSENT_NOTICE,
                "confirmed_at": now_iso(),
            },
            "avatar": {s: None for s in AVATAR_STATES},
            "params": {"speed": 1.0, "temperature": 1.0, "top_k": 15, "top_p": 1.0},
        }
        for sub in ("ref", "avatar", "weights", "dataset"):
            (folder(vid) / sub).mkdir(parents=True, exist_ok=True)
        _write(v)
    return v


def update(vid: str, patch: dict[str, Any]) -> dict[str, Any]:
    allowed = {"name", "language", "params", "base", "avatar_mode"}
    with _lock:
        v = _read(vid)
        for k, val in patch.items():
            if k not in allowed or val is None:
                continue
            if k == "params" and isinstance(val, dict):
                v["params"].update({pk: pv for pk, pv in val.items() if pk in v["params"]})
            elif k == "language" and val in LANGS:
                v["language"] = val
            elif k == "name" and str(val).strip():
                v["name"] = str(val).strip()
            elif k == "base":
                v["base"] = val
            elif k == "avatar_mode" and val in ("circle", "free"):
                v["avatar_mode"] = val
        _write(v)
    return v


def set_ref(vid: str, rel_audio: str, text: str, lang: str, duration: float) -> dict[str, Any]:
    with _lock:
        v = _read(vid)
        v["ref"] = {"audio": rel_audio, "text": text.strip(), "lang": lang, "duration": round(duration, 2)}
        _write(v)
    return v


def add_emotion_ref(vid: str, rel_audio: str, text: str, lang: str, duration: float, emotion: str, source: str = "upload") -> dict[str, Any]:
    """Attach an extra reference clip tagged with an emotion; speak(emotion=...) picks it."""
    if emotion not in EMOTIONS:
        raise ValueError(f"未知的情绪标签：{emotion}（可用：{', '.join(EMOTIONS)}）")
    with _lock:
        v = _read(vid)
        refs = v.setdefault("refs", [])
        rid = uuid.uuid4().hex[:6]
        refs.append({"id": rid, "audio": rel_audio, "text": text.strip(), "lang": lang, "duration": round(duration, 2),
                     "emotion": emotion, "source": source, "added_at": now_iso()})
        _write(v)
    return v


def remove_emotion_ref(vid: str, rid: str) -> dict[str, Any]:
    with _lock:
        v = _read(vid)
        refs = v.get("refs") or []
        hit = next((r for r in refs if r["id"] == rid), None)
        if not hit:
            raise KeyError(rid)
        v["refs"] = [r for r in refs if r["id"] != rid]
        _write(v)
    (folder(vid) / hit["audio"]).unlink(missing_ok=True)
    return v


def pick_ref(v: dict[str, Any], emotion: str | None) -> dict[str, Any] | None:
    """Reference clip for an emotion: tagged clips first (longest wins), else the main reference."""
    if emotion and emotion != "neutral":
        tagged = [r for r in v.get("refs") or [] if r.get("emotion") == emotion]
        if tagged:
            return max(tagged, key=lambda r: r.get("duration", 0))
    if emotion == "neutral":
        tagged = [r for r in v.get("refs") or [] if r.get("emotion") == "neutral"]
        if tagged and not v.get("ref"):
            return max(tagged, key=lambda r: r.get("duration", 0))
    return v.get("ref")


def emotions_of(v: dict[str, Any]) -> list[str]:
    tags = {r["emotion"] for r in v.get("refs") or []}
    if v.get("ref"):
        tags.add("neutral")
    return [e for e in EMOTIONS if e in tags]


def set_weights(vid: str, gpt: str | None, sovits: str | None) -> dict[str, Any]:
    with _lock:
        v = _read(vid)
        v["weights"] = {"gpt": gpt, "sovits": sovits, "trained_at": now_iso() if (gpt or sovits) else None}
        _write(v)
    return v


def set_avatar(vid: str, state: str, rel: str | None) -> dict[str, Any]:
    if state not in AVATAR_STATES:
        raise ValueError(f"未知的形象状态：{state}")
    with _lock:
        v = _read(vid)
        old = v["avatar"].get(state)
        v["avatar"][state] = rel
        _write(v)
    if old and old != rel and old not in v["avatar"].values():
        (folder(vid) / old).unlink(missing_ok=True)
    return v


def delete(vid: str) -> Path:
    """Move the voice folder to data/trash/ (recoverable by moving it back)."""
    d = folder(vid)
    if not (d / "voice.json").exists():
        raise KeyError(vid)
    trash = config.DATA_DIR / "trash"
    trash.mkdir(parents=True, exist_ok=True)
    dst = trash / f"{vid}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.move(str(d), str(dst))
    bus.publish("voices_changed", id=vid)
    return dst


def abspath(v: dict[str, Any], rel: str | None) -> str | None:
    return str((folder(v["id"]) / rel).resolve()) if rel else None
