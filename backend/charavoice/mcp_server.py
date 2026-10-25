"""MCP server exposing CharaVoice to agents: speak, list_voices, listen, transcribe.

It is a thin client of the running CharaVoice backend, so the UI ring reacts to
whatever the agent says or hears. Two transports: stdio (`charavoice-mcp`, or
python -m charavoice.mcp_server) and Streamable HTTP at <backend>/mcp.

Each agent is told apart by name so it can have its own voice: the CHARAVOICE_AGENT
environment variable (stdio) or X-CharaVoice-Agent header (HTTP) if set, otherwise
the client name the MCP client reports when it connects.
"""

from __future__ import annotations

import os
import sys

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .client import ApiFailure, BackendDown, call, connect

server = MCPServer(
    name="charavoice",
    title="CharaVoice",
    instructions=(
        "CharaVoice gives you a voice and ears on the user's computer. "
        "Use speak(text) to say something aloud in the selected character voice "
        "(Chinese, English or Japanese), list_voices() to see available voices, and "
        "listen() to hear one utterance from the user's microphone and get its text. "
        "Keep spoken text short and conversational; do not read code or long lists aloud."
    ),
)


def _agent(ctx: Context | None) -> str | None:
    if os.environ.get("CHARAVOICE_AGENT"):
        return os.environ["CHARAVOICE_AGENT"]
    if ctx is None:
        return None
    try:
        h = ctx.headers or {}
        if h.get("x-charavoice-agent"):
            return h["x-charavoice-agent"]
        params = ctx.session.client_params
        info = getattr(params, "client_info", None) if params else None
        return getattr(info, "name", None)
    except Exception:  # no request context (e.g. called directly in tests)
        return None


def _guard(fn):
    try:
        return fn()
    except (BackendDown, ApiFailure) as e:
        raise ToolError(str(e)) from None


@server.tool(description="Say text aloud through the user's speakers in a CharaVoice character voice. "
                         "language: zh / en / ja / auto (default: detected from the text). "
                         "wait=true blocks until playback finishes. Audio streams sentence by sentence, so it starts in well under a second; "
                         "low_latency=true starts even sooner at slightly lower quality. "
                         "emotion: neutral / happy / angry / sad / surprised / fearful / gentle - uses the voice's reference clip "
                         "tagged with that emotion (list_voices shows which ones each voice has); falls back to neutral. "
                         "Leave voice empty to use the voice the user assigned to you in CharaVoice. "
                         "Optional voice tweaks on top of the voice's saved settings: pitch (semitones -6..6, timbre kept), "
                         "formant (percent -20..20, + sounds younger / more feminine), brightness (dB -6..6), breath (0..1), "
                         "or preset: sweet / deep / younger / mature. "
                         "naturalness: true (pauses by punctuation, breaths, loudness, light room) / false (plain output); default from settings.")
def speak(text: str, voice: str | None = None, language: str | None = None, wait: bool = False, low_latency: bool = False,
          emotion: str | None = None, pitch: float | None = None, formant: float | None = None, brightness: float | None = None,
          breath: float | None = None, preset: str | None = None, naturalness: bool | None = None, ctx: Context | None = None) -> str:
    agent = _agent(ctx)

    def run():
        with connect(timeout=900) as c:
            r = call(c, "POST", "/v1/speak", json={"text": text, "voice": voice, "language": language, "wait": wait, "low_latency": low_latency,
                                                   "emotion": emotion, "agent": agent, "via": "mcp", "pitch": pitch, "formant": formant,
                                                   "brightness": brightness, "breath": breath, "preset": preset,
                                                   "naturalness": naturalness}).json()
        state = "finished playing" if r["finished"] else "playing"
        secs = f", {r['audio_seconds']}s of audio" if r.get("audio_seconds") else ""
        lat = f", audio started after {r['latency_ms']} ms" if r.get("latency_ms") is not None else ""
        return f"Spoken with voice '{r['voice']}' ({r['language']}): {state}{secs}{lat}."

    return _guard(run)


@server.tool(description="List the character voices available in CharaVoice (id, name, language, whether fine-tuned, emotions it can speak with).")
def list_voices() -> list[dict]:
    def run():
        with connect() as c:
            data = call(c, "GET", "/v1/voices").json()["data"]
        return [
            {"id": v["id"], "name": v["name"], "language": v["language"], "trained": v["trained"], "ready": v["ready"],
             "default": v.get("default", False), "emotions": v.get("emotions", [])}
            for v in data
        ]

    return _guard(run)


@server.tool(description="Listen to the user's microphone for one utterance and return the recognized text and language. "
                         "Waits for any ongoing speech playback to finish first. timeout: seconds to wait for the user to start talking.")
def listen(timeout: float = 15.0) -> dict:
    def run():
        with connect(timeout=timeout + 90) as c:
            r = call(c, "POST", "/v1/listen", json={"timeout": timeout, "max_seconds": 60}).json()
        if r.get("timeout"):
            return {"text": "", "language": "", "timed_out": True}
        return {"text": r["text"], "language": r["language"], "timed_out": False}

    return _guard(run)


AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".webm", ".mp4", ".mkv", ".mov"}


@server.tool(description="Transcribe an audio or video file on this computer to text (zh / en / ja / ko / yue, auto-detected). "
                         "path: absolute path to the file. Returns text, language and per-segment timings.")
def transcribe(path: str, language: str | None = None) -> dict:
    from pathlib import Path

    def run():
        p = Path(path).expanduser()
        if not p.is_file():
            raise ToolError(f"File not found: {path}")
        if p.suffix.lower() not in AUDIO_EXT:
            raise ToolError(f"Not an audio/video file: {p.suffix}")
        if p.stat().st_size > 512 * 1024 * 1024:
            raise ToolError("File is larger than 512 MB.")
        with connect(timeout=1800) as c, open(p, "rb") as f:
            r = call(c, "POST", "/v1/audio/transcriptions", files={"file": (p.name, f)},
                     data={"response_format": "verbose_json", "language": language or ""}).json()
        return {"text": r["text"], "language": r["language"], "duration": r["duration"],
                "segments": [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in r["segments"]]}

    return _guard(run)


def main() -> None:
    for stream in (sys.stderr,):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    server.run("stdio")


if __name__ == "__main__":
    main()
