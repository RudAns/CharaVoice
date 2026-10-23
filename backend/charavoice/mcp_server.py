"""MCP server (stdio) exposing CharaVoice to agents: speak, list_voices, listen.

It is a thin client of the running CharaVoice backend, so the UI ring reacts to
whatever the agent says or hears. Run with: python -m charavoice.mcp_server
"""

from __future__ import annotations

import sys

from mcp.server.mcpserver import MCPServer
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


def _guard(fn):
    try:
        return fn()
    except (BackendDown, ApiFailure) as e:
        raise ToolError(str(e)) from None


@server.tool(description="Say text aloud through the user's speakers in a CharaVoice character voice. "
                         "language: zh / en / ja / auto (default: detected from the text). "
                         "wait=true blocks until playback finishes.")
def speak(text: str, voice: str | None = None, language: str | None = None, wait: bool = False) -> str:
    def run():
        with connect(timeout=900) as c:
            r = call(c, "POST", "/v1/speak", json={"text": text, "voice": voice, "language": language, "wait": wait}).json()
        state = "finished playing" if r["finished"] else "playing"
        secs = f", {r['audio_seconds']}s of audio" if r.get("audio_seconds") else ""
        return f"Spoken with voice '{r['voice']}' ({r['language']}): {state}{secs}."

    return _guard(run)


@server.tool(description="List the character voices available in CharaVoice (id, name, language, whether fine-tuned).")
def list_voices() -> list[dict]:
    def run():
        with connect() as c:
            data = call(c, "GET", "/v1/voices").json()["data"]
        return [
            {"id": v["id"], "name": v["name"], "language": v["language"], "trained": v["trained"], "ready": v["ready"], "default": v.get("default", False)}
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


def main() -> None:
    for stream in (sys.stderr,):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    server.run("stdio")


if __name__ == "__main__":
    main()
