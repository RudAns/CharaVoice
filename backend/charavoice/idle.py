"""Give the GPU back when CharaVoice is not being used.

The backend may run in the background for agents (MCP) without the app window. After
`idle.unload_minutes` without any synthesis, training or recording, the voice engines are
stopped, which frees their GPU memory (GPT-SoVITS holds about 3.5 GB); the next request starts
the engine again (the first sentence then takes 20-30 s). After `idle.exit_minutes` with no app
window connected either, the backend exits; an agent's next MCP call starts it again.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from typing import Any

from . import config, procutil

log = logging.getLogger("charavoice.idle")
_last = time.monotonic()
ui_clients = 0  # open /v1/events connections (app windows)
started = time.time()
unloaded_at: float | None = None


def touch() -> None:
    """Something used the voice (synthesis, training, recording)."""
    global _last, unloaded_at
    _last = time.monotonic()
    unloaded_at = None


def idle_seconds() -> float:
    return time.monotonic() - _last


def _busy() -> bool:
    from . import dataset, simple, training
    from .player import player
    from .recorder import recorder

    if player.busy:
        return True
    if recorder.current and not recorder.current.stopped.is_set():
        return True
    for jobs in (training.jobs, simple.jobs, dataset.jobs):
        for j in jobs.values():
            if getattr(j, "status", None) == "running" or getattr(j, "state", None) == "running":
                return True
    return False


def gpu() -> dict[str, Any]:
    """Total GPU memory in use on the card (per-process numbers are not available under WDDM)."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5, creationflags=procutil.CREATE_FLAGS).stdout
        name, used, total = [x.strip() for x in out.splitlines()[0].split(",")]
        return {"name": name, "used_mb": int(used), "total_mb": int(total)}
    except Exception:
        return {}


async def unload(reason: str = "") -> bool:
    """Stop every running voice engine (frees its GPU memory)."""
    global unloaded_at
    from .engine import ENGINES, STOPPED

    stopped = False
    for e in ENGINES.values():
        if e.state != STOPPED:
            log.info("unloading %s %s", e.label, reason)
            await e.stop()
            stopped = True
    if stopped:
        unloaded_at = time.time()
    return stopped


async def shutdown() -> None:
    """Stop the engines and end the backend process."""
    await unload("(service stopping)")
    config.clear_runtime(os.getpid())
    log.info("backend exiting")
    os._exit(0)


async def loop() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            if _busy():
                touch()
                continue
            s = config.load_settings().get("idle", {})
            idle = idle_seconds()
            unload_after = float(s.get("unload_minutes", 10)) * 60
            exit_after = float(s.get("exit_minutes", 60)) * 60
            if unload_after > 0 and idle >= unload_after and unloaded_at is None:
                await unload(f"after {idle / 60:.0f} idle minutes")
            if exit_after > 0 and idle >= exit_after and ui_clients == 0:
                log.info("no use for %.0f minutes and no app window: exiting", idle / 60)
                await shutdown()
        except Exception:
            log.exception("idle check failed")


def status() -> dict[str, Any]:
    from .engine import ENGINES

    s = config.load_settings().get("idle", {})
    return {
        "pid": os.getpid(),
        "uptime": round(time.time() - started),
        "idle_seconds": round(idle_seconds()),
        "app_windows": ui_clients,
        "engines": [{"name": e.name, "label": e.label, "state": e.state} for e in ENGINES.values()],
        "unloaded_at": unloaded_at,
        "unload_minutes": s.get("unload_minutes", 10),
        "exit_minutes": s.get("exit_minutes", 60),
        "gpu": gpu(),
    }
