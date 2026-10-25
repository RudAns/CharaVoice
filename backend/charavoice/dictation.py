"""Global dictation: listen for one turn, then type the text into whatever window has focus.

The desktop shell triggers it with a global hotkey and shows a small non-focusable widget,
so the user's own window keeps keyboard focus and receives the text. Typing uses Win32
SendInput with KEYEVENTF_UNICODE, which works for Chinese and Japanese without an IME.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import asr, config
from .events import bus
from .player import player

log = logging.getLogger("charavoice.dictation")


def type_text(text: str) -> None:
    import ctypes
    from ctypes import wintypes

    INPUT_KEYBOARD, KEYEVENTF_UNICODE, KEYEVENTF_KEYUP = 1, 0x0004, 0x0002

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    units = text.encode("utf-16-le")
    codes = [int.from_bytes(units[i : i + 2], "little") for i in range(0, len(units), 2)]
    seq = []
    for c in codes:
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = INPUT(type=INPUT_KEYBOARD)
            inp.u.ki = KEYBDINPUT(0, c, flags, 0, 0)
            seq.append(inp)
    arr = (INPUT * len(seq))(*seq)
    sent = ctypes.windll.user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))
    if sent != len(seq):
        raise RuntimeError("输入被系统拦截（目标窗口可能以管理员身份运行）")


class Dictation:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.cancelled = False

    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()

    def toggle(self, type_into_focus: bool = True) -> bool:
        if self.active:
            self.cancelled = True
            return False
        self.cancelled = False
        self.task = asyncio.create_task(self._run(type_into_focus))
        return True

    async def _run(self, type_into_focus: bool) -> None:
        s = config.load_settings()["listen"]
        # The turn ends this long after the last word. The VAD itself needs min_silence to close a
        # segment, so that is the floor; the rest of the wait is measured from the true speech end.
        end_silence = float(s.get("dictation_end_silence", 0.7))
        timeout = float(s.get("dictation_timeout", 8.0))
        await asyncio.to_thread(player.wait_idle, 60)
        q = bus.subscribe()
        asr.listener.acquire()
        bus.publish("dictation", state="listening")
        parts: list[str] = []
        lang = ""
        started = in_speech = False
        pending = 0
        spoke_until = 0.0  # monotonic time at which the last segment's speech really ended
        timing: dict[str, float] = {}
        t0 = time.monotonic()
        try:
            while not self.cancelled:
                now = time.monotonic()
                if parts and not in_speech and pending == 0 and now - spoke_until >= end_silence:
                    break
                if not started and now - t0 > timeout:
                    bus.publish("dictation", state="done", text="", reason="没有听到说话")
                    return
                try:
                    ev = await asyncio.wait_for(q.get(), 0.03)
                except asyncio.TimeoutError:
                    continue
                t = ev["type"]
                if t == "speech_start":
                    started = in_speech = True
                elif t == "speech_end":
                    in_speech = False
                    if not ev.get("discarded"):
                        pending += 1
                        lag = float(ev.get("end_lag", 0.0))
                        spoke_until = time.monotonic() - lag
                        timing["vad_ms"] = round(lag * 1000)
                elif t == "partial":
                    bus.publish("dictation", state="listening", partial=" ".join(parts + [ev["text"]]))
                elif t == "transcript":
                    pending = max(0, pending - 1)
                    timing["asr_ms"] = ev.get("asr_ms", 0)
                    timing["result_ms"] = round((time.monotonic() - spoke_until) * 1000)
                    if not ev.get("empty"):
                        parts.append(ev["text"])
                        lang = lang or ev["language"]
                        bus.publish("dictation", state="listening", partial=" ".join(parts))
                elif t == "listen_error":
                    bus.publish("dictation", state="error", message=ev["message"])
                    return
            if self.cancelled:
                bus.publish("dictation", state="done", text="", reason="已取消")
                return
            joiner = "" if lang in ("zh", "ja", "yue") else " "
            text = joiner.join(parts).strip()
            if text and type_into_focus:
                bus.publish("dictation", state="typing", text=text)
                t1 = time.perf_counter()
                try:
                    await asyncio.to_thread(type_text, text)
                except Exception as e:
                    bus.publish("dictation", state="error", message=f"无法输入文字：{e}")
                    return
                timing["type_ms"] = round((time.perf_counter() - t1) * 1000)
            timing["total_ms"] = round((time.monotonic() - spoke_until) * 1000)
            log.info("dictation %s", timing)
            bus.publish("dictation", state="done", text=text, language=lang, timing=timing)
        finally:
            asr.listener.release()
            bus.unsubscribe(q)


dictation = Dictation()
