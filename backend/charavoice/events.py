"""In-process event bus and the presence state shown by the UI ring.

Audio threads publish from outside the event loop, so `publish` is thread-safe:
it hops onto the loop with call_soon_threadsafe. Slow subscribers drop events
instead of blocking producers (level frames are only useful when fresh).
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

# Presence states, in priority order (highest first) when several are active.
SPEAKING = "speaking"
LISTENING = "listening"
THINKING = "thinking"
IDLE = "idle"
_PRIORITY = [SPEAKING, THINKING, LISTENING]


class EventBus:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._active: dict[str, int] = {s: 0 for s in _PRIORITY}
        self.state = IDLE

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, maxsize: int = 256) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _deliver(self, event: dict[str, Any]) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def publish(self, type_: str, **data: Any) -> None:
        event = {"type": type_, "t": round(time.time(), 3), **data}
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._deliver(event)
        else:
            loop.call_soon_threadsafe(self._deliver, event)

    # Presence: each activity holds a counter so overlapping work composes.
    def enter(self, state: str) -> None:
        with self._lock:
            self._active[state] += 1
        self._recompute()

    def leave(self, state: str) -> None:
        with self._lock:
            self._active[state] = max(0, self._active[state] - 1)
        self._recompute()

    def _recompute(self) -> None:
        with self._lock:
            new = next((s for s in _PRIORITY if self._active[s] > 0), IDLE)
            changed = new != self.state
            self.state = new
        if changed:
            self.publish("state", state=new)


bus = EventBus()
