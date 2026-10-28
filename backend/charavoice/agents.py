"""Per-agent voice binding.

Every caller may name itself: MCP clients are named by the client info they send when they
connect (for example "claude-code"), HTTP callers with the `X-CharaVoice-Agent` header or an
`agent` field. Each agent seen is remembered in data/agents.json, and the user can give it its
own voice and default emotion; a request that names no voice then speaks with that binding.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from . import config

FILE = config.DATA_DIR / "agents.json"
_lock = threading.Lock()
_NAME = re.compile(r"[^\w .@:/+-]")


def clean(name: str | None) -> str:
    """Normalise a self-reported agent name (it is untrusted text; keep it short and printable)."""
    return _NAME.sub("", (name or "").strip())[:64]


def _load() -> dict[str, dict[str, Any]]:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict[str, dict[str, Any]]) -> None:
    config.ensure_dirs()
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(FILE)


def all_agents() -> list[dict[str, Any]]:
    with _lock:
        data = _load()
    return sorted(({"name": k, **v} for k, v in data.items()), key=lambda a: -a.get("last_seen", 0))


def touch(name: str | None, via: str) -> dict[str, Any] | None:
    """Record a call from `name` and return its binding (None for anonymous callers)."""
    name = clean(name)
    if not name:
        return None
    from . import idle

    idle.agent_touch()
    with _lock:
        data = _load()
        a = data.setdefault(name, {"voice": None, "emotion": None, "first_seen": time.time(), "calls": 0})
        a["last_seen"] = time.time()
        a["calls"] = a.get("calls", 0) + 1
        a["via"] = via
        _save(data)
        return dict(a)


def update(name: str, patch: dict[str, Any]) -> dict[str, Any]:
    name = clean(name)
    with _lock:
        data = _load()
        if name not in data:
            raise LookupError(f"没有这个 Agent：{name}")
        for k in ("voice", "emotion"):
            if k in patch:
                data[name][k] = patch[k] or None
        _save(data)
        return {"name": name, **data[name]}


def remove(name: str) -> None:
    with _lock:
        data = _load()
        data.pop(clean(name), None)
        _save(data)
