"""MCP server exposing CharaVoice to agents: list_voices, speak, synthesize, stop.

The voices are the material-library folders the user trained in CharaVoice (Japanese). It is a thin
client of the CharaVoice backend; with stdio it starts that backend in the background when it is
not running yet (see client.connect), and reuses it when the app is already open, so models are
loaded once. Two transports: stdio (`charavoice-mcp`, or `charavoice-backend.exe mcp` in the
installed app) and Streamable HTTP at <backend>/mcp.

Each agent is told apart by name so it can have its own voice: the CHARAVOICE_AGENT environment
variable (stdio) or the X-CharaVoice-Agent header (HTTP) if set, otherwise the client name the MCP
client reports when it connects.
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
        "CharaVoice speaks Japanese in character voices the user cloned on this computer. "
        "list_voices() shows the voices; speak(text) says Japanese text aloud on the user's speakers "
        "(in the voice the user assigned to you, or the default voice); synthesize(text) writes an audio "
        "file instead and returns its path; stop() stops speech that is playing. "
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


@server.tool(description="List the character voices the user trained in CharaVoice. Each entry: voice (the id to pass "
                         "to speak / synthesize), name, status (ready = can speak), default (used when you pass no voice). "
                         "The voices speak Japanese.")
def list_voices(ctx: Context | None = None) -> list[dict]:
    agent = _agent(ctx)

    def run():
        with connect() as c:
            return call(c, "GET", "/v1/simple/mcp/voices", params={"agent": agent or ""}).json()["voices"]

    return _guard(run)


@server.tool(description="Say Japanese text aloud on the user's speakers in a CharaVoice character voice. "
                         "text: what to say (Japanese). voice: id or name from list_voices; leave empty to use the voice "
                         "the user assigned to you in CharaVoice, otherwise the default voice. "
                         "wait: true blocks until playback finishes (default false: returns as soon as audio starts, "
                         "usually well under a second). A new speak call does not interrupt; call stop() first to cut "
                         "off speech that is still playing.")
def speak(text: str, voice: str | None = None, wait: bool = False, ctx: Context | None = None) -> str:
    agent = _agent(ctx)

    def run():
        with connect(timeout=900) as c:
            r = call(c, "POST", "/v1/speak", json={"text": text, "voice": voice, "wait": wait, "agent": agent, "via": "mcp"}).json()
        state = "finished playing" if r["finished"] else "playing"
        secs = f", {r['audio_seconds']}s of audio" if r.get("audio_seconds") else ""
        lat = f", audio started after {r['latency_ms']} ms" if r.get("latency_ms") is not None else ""
        return f"Spoken with voice '{r['voice']}': {state}{secs}{lat}."

    return _guard(run)


@server.tool(description="Synthesize Japanese text to an audio file in a CharaVoice character voice instead of playing it. "
                         "text: what to say (Japanese, articles are fine). voice: id or name from list_voices; empty = "
                         "your assigned voice or the default voice. format: wav (default) or mp3. Returns the absolute path "
                         "of the file on this computer and its length. The audio carries an AI-generated tag and watermark.")
def synthesize(text: str, voice: str | None = None, format: str = "wav", ctx: Context | None = None) -> dict:
    agent = _agent(ctx)

    def run():
        with connect(timeout=1800) as c:
            r = call(c, "POST", "/v1/simple/synthesize", json={"text": text, "voice": voice, "format": format, "agent": agent}).json()
        return {"path": r["path"], "seconds": r["seconds"], "voice": r["voice"], "format": r["format"]}

    return _guard(run)


@server.tool(description="Stop any CharaVoice speech that is playing right now (from you or anyone else).")
def stop() -> str:
    def run():
        with connect() as c:
            n = call(c, "POST", "/v1/audio/stop").json().get("stopped", 0)
        return f"Stopped {n} utterance(s)." if n else "Nothing was playing."

    return _guard(run)


def main() -> None:
    for stream in (sys.stderr,):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    server.run("stdio")


if __name__ == "__main__":
    main()
