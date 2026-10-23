"""CharaVoice HTTP / WebSocket API (FastAPI), bound to 127.0.0.1 only.

Authentication: every /v1 route needs the token from data/runtime.json, sent as
`Authorization: Bearer <token>` (works as an OpenAI api_key), the
`X-CharaVoice-Token` header, or a `token` query parameter (for <img>/WebSocket).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from . import APP_NAME, __version__, asr, audio_util, config, speech, voices
from .engine import EngineUnavailable, engine
from .events import bus
from .player import player

log = logging.getLogger("charavoice.server")
RUNTIME: dict[str, Any] = {}
OPENAI_VOICE_NAMES = {"alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse"}
OPENAI_MODEL_NAMES = {"tts-1", "tts-1-hd", "gpt-4o-mini-tts", "whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe"}


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str = "bad_request"):
        self.status, self.message, self.code = status, message, code


def _err(status: int, message: str, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "code": code, "type": code}})


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    bus.bind(asyncio.get_running_loop())
    config.ensure_dirs()
    if config.load_settings()["engine"]["autostart"] and engine.installed():
        asyncio.create_task(engine.start())
    yield
    player.stop_all()
    await engine.stop()
    config.clear_runtime(os.getpid())


app = FastAPI(title=APP_NAME, version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://tauri.localhost", "https://tauri.localhost", "tauri://localhost", "http://localhost:1420"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-CharaVoice-Voice", "X-CharaVoice-Played", "X-CharaVoice-AI-Generated"],
)


@app.exception_handler(ApiError)
async def _api_error(_: Request, e: ApiError):
    return _err(e.status, e.message, e.code)


@app.exception_handler(EngineUnavailable)
async def _engine_error(_: Request, e: EngineUnavailable):
    status = 400 if e.code == "bad_request" else 503
    return _err(status, str(e), e.code)


@app.exception_handler(LookupError)
async def _lookup_error(_: Request, e: LookupError):
    return _err(404, str(e).strip("'\""), "not_found")


@app.exception_handler(ValueError)
async def _value_error(_: Request, e: ValueError):
    return _err(400, str(e), "bad_request")


def _token_ok(given: str | None) -> bool:
    expected = RUNTIME.get("token", "")
    return bool(given) and bool(expected) and hmac.compare_digest(given, expected)


def auth(request: Request, token: str | None = Query(default=None)) -> None:
    h = request.headers.get("authorization", "")
    bearer = h[7:].strip() if h.lower().startswith("bearer ") else None
    if not (_token_ok(bearer) or _token_ok(request.headers.get("x-charavoice-token")) or _token_ok(token)):
        raise ApiError(401, "缺少或错误的访问口令。请从 data/runtime.json 读取 token，并以 Authorization: Bearer <token> 发送。", "unauthorized")


# ---------------------------------------------------------------- status
@app.get("/health")
async def health():
    return {"ok": True, "name": APP_NAME, "version": __version__, "engine": engine.state, "state": bus.state}


@app.get("/v1/status", dependencies=[Depends(auth)])
async def status():
    return {
        "version": __version__,
        "state": bus.state,
        "engine": engine.info(),
        "listening": asr.listener.active,
        "playing": player.busy,
        "voices": len(voices.list_all()),
    }


@app.get("/v1/engine", dependencies=[Depends(auth)])
async def engine_info():
    return engine.info()


@app.post("/v1/engine/start", dependencies=[Depends(auth)])
async def engine_start():
    asyncio.create_task(engine.start())
    await asyncio.sleep(0.1)
    return engine.info()


@app.post("/v1/engine/stop", dependencies=[Depends(auth)])
async def engine_stop():
    await engine.stop()
    return engine.info()


@app.get("/v1/engine/logs", dependencies=[Depends(auth)])
async def engine_logs(lines: int = 200):
    return {"lines": list(engine.logs)[-max(1, min(lines, 400)) :]}


@app.get("/v1/settings", dependencies=[Depends(auth)])
async def get_settings():
    return config.load_settings()


@app.patch("/v1/settings", dependencies=[Depends(auth)])
async def patch_settings(patch: dict[str, Any]):
    return config.save_settings(patch)


@app.get("/v1/devices", dependencies=[Depends(auth)])
async def devices():
    import sounddevice as sd

    out = []
    for i, d in enumerate(sd.query_devices()):
        api = sd.query_hostapis(d["hostapi"])["name"]
        out.append({"index": i, "name": d["name"], "hostapi": api, "inputs": d["max_input_channels"], "outputs": d["max_output_channels"]})
    return {"devices": out, "default": list(sd.default.device)}


# ---------------------------------------------------------------- voices
def _voice_public(v: dict) -> dict:
    base = f"/v1/voices/{v['id']}/avatar"
    return {
        "id": v["id"],
        "object": "voice",
        "name": v["name"],
        "language": v.get("language"),
        "base": v.get("base"),
        "ready": bool(v.get("ref")),
        "trained": bool((v.get("weights") or {}).get("sovits") or (v.get("weights") or {}).get("gpt")),
        "ref": v.get("ref"),
        "consent": v.get("consent"),
        "params": v.get("params"),
        "created_at": v.get("created_at"),
        "avatar": {
            s: ({"url": f"{base}/{s}?v={Path(p).stem}", "kind": "video" if Path(p).suffix in (".webm", ".mp4") else "image"} if p else None)
            for s, p in (v.get("avatar") or {}).items()
        },
        "avatar_mode": v.get("avatar_mode", "circle"),
    }


@app.get("/v1/voices", dependencies=[Depends(auth)])
async def list_voices():
    default = config.load_settings()["speech"]["default_voice"]
    data = [_voice_public(v) | {"default": v["id"] == default} for v in voices.list_all()]
    if data and not any(d["default"] for d in data):
        data[0]["default"] = True
    return {"object": "list", "data": data}


async def _save_ref(v: dict, upload: UploadFile, text: str, lang: str) -> dict:
    raw = await upload.read()
    if not raw:
        raise ApiError(400, "参考音频是空文件。")
    rel = "ref/ref.wav"
    dst = voices.folder(v["id"]) / rel
    dur = await asyncio.to_thread(audio_util.to_wav_file, raw, dst, 32000)
    if dur < 3 or dur > 10:
        dst.unlink(missing_ok=True)
        raise ApiError(400, f"参考音频时长 {dur:.1f} 秒，需要在 3-10 秒之间（GPT-SoVITS 的要求）。")
    text = (text or "").strip()
    if not text or not lang:
        samples = await asyncio.to_thread(audio_util.decode, dst, 16000)
        r = await asyncio.to_thread(asr.transcribe, samples)
        text = text or r["text"]
        lang = lang or (r["language"] if r["language"] in voices.LANGS else v.get("language", "zh"))
    lang = speech.norm_lang(lang, text)
    return voices.set_ref(v["id"], rel, text, lang, dur)


@app.post("/v1/voices", dependencies=[Depends(auth)])
async def create_voice(
    name: str = Form(...),
    consent: str = Form(..., description="self | authorized"),
    consent_ack: bool = Form(False, description="必须为 true：确认已告知被模仿的人并取得其单独同意"),
    language: str = Form("zh"),
    base: str | None = Form(None),
    ref_audio: UploadFile | None = File(None),
    ref_text: str = Form(""),
    ref_lang: str = Form(""),
):
    if consent not in voices.CONSENT_KINDS:
        raise ApiError(400, "新建声音前必须选择“本人声音”(self) 或“已获授权的声音”(authorized)。", "consent_required")
    if not consent_ack:
        raise ApiError(400, voices.CONSENT_NOTICE + "确认后请把 consent_ack 设为 true。", "consent_required")
    if base:
        engine.base_weights(base)
    v = voices.create(name, consent, language, base)
    if ref_audio is not None:
        try:
            v = await _save_ref(v, ref_audio, ref_text, ref_lang)
        except Exception:
            voices.delete(v["id"])
            raise
    return _voice_public(v)


@app.get("/v1/voices/{vid}", dependencies=[Depends(auth)])
async def get_voice(vid: str):
    try:
        return _voice_public(voices.get(vid))
    except KeyError:
        raise ApiError(404, f"找不到声音 {vid}", "not_found")


class VoicePatch(BaseModel):
    name: str | None = None
    avatar_mode: str | None = None
    language: str | None = None
    base: str | None = None
    params: dict[str, float] | None = None
    default: bool | None = None


@app.patch("/v1/voices/{vid}", dependencies=[Depends(auth)])
async def patch_voice(vid: str, body: VoicePatch):
    try:
        if body.base:
            engine.base_weights(body.base)
        v = voices.update(vid, body.model_dump(exclude={"default"}))
    except KeyError:
        raise ApiError(404, f"找不到声音 {vid}", "not_found")
    if body.default:
        config.save_settings({"speech": {"default_voice": vid}})
    return _voice_public(v)


@app.delete("/v1/voices/{vid}", dependencies=[Depends(auth)])
async def delete_voice(vid: str):
    try:
        dst = voices.delete(vid)
    except KeyError:
        raise ApiError(404, f"找不到声音 {vid}", "not_found")
    return {"deleted": vid, "moved_to": str(dst.relative_to(config.ROOT)).replace("\\", "/")}


@app.post("/v1/voices/{vid}/ref", dependencies=[Depends(auth)])
async def set_ref(vid: str, ref_audio: UploadFile = File(...), ref_text: str = Form(""), ref_lang: str = Form("")):
    try:
        v = voices.get(vid)
    except KeyError:
        raise ApiError(404, f"找不到声音 {vid}", "not_found")
    return _voice_public(await _save_ref(v, ref_audio, ref_text, ref_lang))


@app.get("/v1/voices/{vid}/ref/audio", dependencies=[Depends(auth)])
async def get_ref_audio(vid: str):
    try:
        v = voices.get(vid)
    except KeyError:
        raise ApiError(404, f"找不到声音 {vid}", "not_found")
    if not v.get("ref"):
        raise ApiError(404, "这个声音还没有参考音频", "not_found")
    return FileResponse(voices.abspath(v, v["ref"]["audio"]), media_type="audio/wav")


@app.put("/v1/voices/{vid}/avatar/{state}", dependencies=[Depends(auth)])
async def put_avatar(vid: str, state: str, file: UploadFile = File(...)):
    try:
        voices.get(vid)
    except KeyError:
        raise ApiError(404, f"找不到声音 {vid}", "not_found")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in voices.AVATAR_EXT:
        raise ApiError(400, "形象文件只支持 GIF、APNG/PNG、WebP、WebM、MP4。")
    raw = await file.read()
    if len(raw) > 64 * 1024 * 1024:
        raise ApiError(400, "形象文件太大（上限 64MB）。")
    rel = f"avatar/{state}-{secrets.token_hex(3)}{ext}"
    (voices.folder(vid) / rel).write_bytes(raw)
    return _voice_public(voices.set_avatar(vid, state, rel))


@app.delete("/v1/voices/{vid}/avatar/{state}", dependencies=[Depends(auth)])
async def delete_avatar(vid: str, state: str):
    return _voice_public(voices.set_avatar(vid, state, None))


@app.get("/v1/voices/{vid}/avatar/{state}", dependencies=[Depends(auth)])
async def get_avatar(vid: str, state: str):
    try:
        v = voices.get(vid)
    except KeyError:
        raise ApiError(404, f"找不到声音 {vid}", "not_found")
    av = v.get("avatar") or {}
    rel = av.get(state) or av.get("default")
    if not rel:
        raise ApiError(404, "没有设置形象", "not_found")
    return FileResponse(voices.abspath(v, rel), headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- speak
class SpeechRequest(BaseModel):
    model: str | None = None
    input: str
    voice: str | None = None
    language: str | None = None
    response_format: str = "wav"
    speed: float | None = Field(default=None, ge=0.25, le=4.0)
    stream: bool = False
    stream_format: str | None = None
    play: bool | None = Field(default=None, description="由 CharaVoice 在本机播放；false 则只返回音频")
    instructions: str | None = None


def _pick_voice(*candidates: str | None) -> str | None:
    for c in candidates:
        if not c or c in OPENAI_VOICE_NAMES or c in OPENAI_MODEL_NAMES:
            continue
        return voices.find(c)["id"]
    return None


def _ai_headers(job: speech.SpeakJob) -> dict[str, str]:
    return {
        "X-CharaVoice-Voice": job.voice["id"],
        "X-CharaVoice-Played": "true" if job.play else "false",
        "X-CharaVoice-AI-Generated": "true",
    }


@app.post("/v1/audio/speech", dependencies=[Depends(auth)])
async def audio_speech(body: SpeechRequest):
    """OpenAI-compatible TTS. `model` or `voice` = CharaVoice voice id or name."""
    vid = _pick_voice(body.voice, body.model)
    streaming = body.stream or body.stream_format == "audio"
    fmt = (body.response_format or "wav").lower()
    if streaming and fmt not in ("wav", "pcm"):
        raise ApiError(400, "流式输出只支持 response_format=wav 或 pcm。")
    job = speech.start(body.input, vid, body.language, play=body.play, speed=body.speed, live=streaming)
    if streaming:
        from . import compliance

        async def gen():
            first = True
            async for sr, pcm in speech.stream(job):
                if first and fmt == "wav":
                    yield compliance.wav_stream_header(sr)
                first = False
                yield pcm.astype("<i2").tobytes()
            if job.error:
                log.warning("stream ended with error: %s", job.error)

        mime = "audio/wav" if fmt == "wav" else "audio/pcm"
        return StreamingResponse(gen(), media_type=mime, headers=_ai_headers(job))
    await job.done.wait()
    if job.error:
        raise EngineUnavailable(job.error, "synthesis_failed")
    from . import compliance

    audio = compliance.finalize(job.audio(), job.sr)
    data, mime = await asyncio.to_thread(compliance.encode_tagged, audio, job.sr, fmt, job.voice)
    return Response(data, media_type=mime, headers=_ai_headers(job))


class SpeakBody(BaseModel):
    text: str
    voice: str | None = None
    language: str | None = None
    speed: float | None = Field(default=None, ge=0.25, le=4.0)
    wait: bool = Field(default=False, description="等到播放结束再返回")


@app.post("/v1/speak", dependencies=[Depends(auth)])
async def speak(body: SpeakBody):
    """Convenience endpoint for agents/CLI: synthesize and play locally, return JSON."""
    vid = _pick_voice(body.voice)
    t0 = time.monotonic()
    job = speech.start(body.text, vid, body.language, play=True, speed=body.speed)
    utt = job.utterance
    assert utt is not None
    started = await asyncio.to_thread(_wait_any, utt, 120.0)
    if job.error:
        raise EngineUnavailable(job.error, "synthesis_failed")
    if body.wait:
        await asyncio.to_thread(utt.done.wait, 600)
        await job.done.wait()
        if job.error:
            raise EngineUnavailable(job.error, "synthesis_failed")
    seconds = round(sum(len(c) for c in job.chunks) / job.sr, 2) if job.sr else None
    return {
        "id": utt.id,
        "voice": job.voice["id"],
        "language": job.request.text_lang,
        "started": started,
        "finished": utt.done.is_set(),
        "audio_seconds": seconds if job.done.is_set() else None,
        "latency_ms": int((time.monotonic() - t0) * 1000),
    }


def _wait_any(utt, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if utt.started.wait(0.05):
            return True
        if utt.done.is_set():
            return False
    return False


@app.post("/v1/audio/stop", dependencies=[Depends(auth)])
async def audio_stop():
    return {"stopped": player.stop_all()}


# ---------------------------------------------------------------- listen
@app.post("/v1/audio/transcriptions", dependencies=[Depends(auth)])
async def transcriptions(
    file: UploadFile = File(...),
    model: str = Form("sensevoice"),
    language: str = Form(""),
    prompt: str = Form(""),
    response_format: str = Form("json"),
    temperature: float = Form(0.0),
):
    raw = await file.read()
    samples = await asyncio.to_thread(audio_util.decode, raw, asr.SR)
    dur = len(samples) / asr.SR
    lang = "" if language in ("", "auto") else speech.norm_lang(language, "")
    if dur <= 25:
        segs = [(0, samples)]
    else:
        segs = await asyncio.to_thread(asr.segment, samples, 0.4, 0.25, 20.0)
    results = []
    for start, seg in segs:
        r = await asyncio.to_thread(asr.transcribe, seg, lang)
        r["start"], r["end"] = round(start / asr.SR, 2), round((start + len(seg)) / asr.SR, 2)
        if r["text"]:
            results.append(r)
    joiner = "" if (results and results[0]["language"] in ("zh", "ja", "yue")) else " "
    text = joiner.join(r["text"] for r in results).strip()
    detected = results[0]["language"] if results else lang
    if response_format == "text":
        return Response(text, media_type="text/plain; charset=utf-8")
    if response_format == "verbose_json":
        return {
            "task": "transcribe",
            "language": detected,
            "duration": round(dur, 2),
            "text": text,
            "segments": [
                {"id": i, "start": r["start"], "end": r["end"], "text": r["text"], "language": r["language"], "emotion": r["emotion"]}
                for i, r in enumerate(results)
            ],
        }
    return {"text": text, "language": detected}


class ListenBody(BaseModel):
    timeout: float = Field(default=15.0, ge=1, le=300, description="多少秒内没有开口就返回空")
    max_seconds: float = Field(default=60.0, ge=2, le=600)


@app.post("/v1/listen", dependencies=[Depends(auth)])
async def listen_once(body: ListenBody):
    """Listen for one utterance from the microphone and return its text."""
    return await _listen_once(body.timeout, body.max_seconds)


async def _listen_once(timeout: float, max_seconds: float) -> dict:
    """One conversational turn: segments separated by short pauses are joined until the
    user stays silent for listen.turn_silence seconds."""
    await asyncio.to_thread(player.wait_idle, 600)
    turn_silence = float(config.load_settings()["listen"].get("turn_silence", 1.2))
    q = bus.subscribe()
    asr.listener.acquire()
    t0 = time.monotonic()
    started = False
    in_speech = False
    pending = 0  # segments ended but not yet transcribed
    parts: list[dict] = []
    last_end = 0.0
    try:
        while True:
            now = time.monotonic()
            if parts and not in_speech and pending == 0 and now - last_end >= turn_silence:
                break
            if not started and now - t0 >= timeout:
                return {"text": "", "language": "", "timeout": True, "heard_speech": False}
            if now - t0 >= max_seconds:
                break
            try:
                ev = await asyncio.wait_for(q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            if ev["type"] == "speech_start":
                started = in_speech = True
            elif ev["type"] == "speech_end":
                in_speech = False
                if not ev.get("discarded"):
                    pending += 1
                last_end = time.monotonic()
            elif ev["type"] == "listen_error":
                raise ApiError(500, ev["message"], "listen_failed")
            elif ev["type"] == "transcript":
                pending = max(0, pending - 1)
                if not ev.get("empty"):
                    parts.append(ev)
        if not parts:
            return {"text": "", "language": "", "timeout": True, "heard_speech": started}
        lang = parts[0]["language"]
        joiner = "" if lang in ("zh", "ja", "yue") else " "
        return {
            "text": joiner.join(p["text"] for p in parts).strip(),
            "language": lang,
            "emotion": parts[0].get("emotion", ""),
            "duration": round(sum(p.get("duration") or 0 for p in parts), 2),
            "segments": len(parts),
            "timeout": False,
        }
    finally:
        asr.listener.release()
        bus.unsubscribe(q)


@app.post("/v1/listen/simulate", dependencies=[Depends(auth)])
async def listen_simulate(file: UploadFile = File(...)):
    """Testing aid: play an audio file into the listening pipeline as if it came from the microphone."""
    samples = await asyncio.to_thread(audio_util.decode, await file.read(), asr.SR)
    was_active = asr.listener.active
    if not was_active:
        asr.listener.source = "file"
    asr.listener.simulate(samples)
    if not was_active:
        asr.listener.acquire()

        async def release_later():
            await asyncio.sleep(len(samples) / asr.SR + 3.0)
            asr.listener.release()

        asyncio.create_task(release_later())
    return {"queued_seconds": round(len(samples) / asr.SR, 2), "source": "file" if not was_active else asr.listener.source}


def _ws_token_ok(ws: WebSocket) -> bool:
    t = ws.query_params.get("token")
    h = ws.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        t = t or h[7:].strip()
    return _token_ok(t) or _token_ok(ws.headers.get("x-charavoice-token"))


LISTEN_EVENTS = {"speech_start", "speech_end", "transcript", "listen_start", "listen_stop", "listen_error"}


@app.websocket("/v1/listen")
async def ws_listen(ws: WebSocket):
    """Real-time dictation: events speech_start / speech_end / transcript {text, language}."""
    if not _ws_token_ok(ws):
        await ws.close(code=4401, reason="unauthorized")
        return
    await ws.accept()
    with_levels = ws.query_params.get("levels") in ("1", "true")
    q = bus.subscribe()
    asr.listener.acquire()

    async def reader():
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            while True:
                msg = await ws.receive_text()
                with contextlib.suppress(json.JSONDecodeError):
                    if json.loads(msg).get("type") == "stop":
                        return

    rtask = asyncio.create_task(reader())
    try:
        await ws.send_json({"type": "ready", "source": asr.listener.source})
        while not rtask.done():
            try:
                ev = await asyncio.wait_for(q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if ev["type"] in LISTEN_EVENTS or (with_levels and ev["type"] == "level" and ev.get("source") == "mic"):
                await ws.send_json(ev)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        rtask.cancel()
        asr.listener.release()
        bus.unsubscribe(q)
        with contextlib.suppress(Exception):
            await ws.close()


@app.websocket("/v1/events")
async def ws_events(ws: WebSocket):
    """UI feed: presence state, live levels, engine status, speak/listen events."""
    if not _ws_token_ok(ws):
        await ws.close(code=4401, reason="unauthorized")
        return
    await ws.accept()
    q = bus.subscribe(maxsize=512)
    try:
        await ws.send_json({"type": "hello", "state": bus.state, "engine": engine.info(), "listening": asr.listener.active})
        while True:
            ev = await q.get()
            await ws.send_json(ev)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bus.unsubscribe(q)


@app.post("/v1/ui/listen", dependencies=[Depends(auth)])
async def ui_listen(on: bool = True):
    """Toggle continuous listening held by the UI (mic button)."""
    global _ui_holds
    if on and not _ui_holds:
        asr.listener.acquire()
        _ui_holds = True
    elif not on and _ui_holds:
        asr.listener.release()
        _ui_holds = False
    return {"listening": asr.listener.active or _ui_holds}


_ui_holds = False


# ---------------------------------------------------------------- dataset (M3)
from . import dataset  # noqa: E402


def _voice_or_404(vid: str) -> dict:
    try:
        return voices.get(vid)
    except KeyError:
        raise ApiError(404, f"找不到声音 {vid}", "not_found")


@app.get("/v1/voices/{vid}/dataset", dependencies=[Depends(auth)])
async def dataset_get(vid: str):
    _voice_or_404(vid)
    data = await asyncio.to_thread(dataset.load, vid)
    job = dataset.current_job(vid)
    return {
        "items": dataset.public_items(data),
        "summary": dataset.summary(vid, data),
        "target": {k: v for k, v in (data.get("target") or {}).items() if k != "embedding"} or None,
        "sources": data.get("sources", []),
        "job": job.public() if job else None,
    }


@app.post("/v1/voices/{vid}/dataset/import", dependencies=[Depends(auth)])
async def dataset_import(vid: str, files: list[UploadFile] = File(...), separate: bool = Form(True)):
    _voice_or_404(vid)
    payload = [(f.filename or "upload", await f.read()) for f in files]
    job = dataset.start_import(vid, payload, separate)
    return job.public()


@app.post("/v1/voices/{vid}/dataset/cancel", dependencies=[Depends(auth)])
async def dataset_cancel(vid: str):
    return {"cancelled": dataset.cancel(vid)}


class ItemPatch(BaseModel):
    text: str | None = None
    lang: str | None = None
    deleted: bool | None = None


@app.patch("/v1/voices/{vid}/dataset/items/{item_id}", dependencies=[Depends(auth)])
async def dataset_item(vid: str, item_id: str, body: ItemPatch):
    try:
        it = await asyncio.to_thread(dataset.edit_item, vid, item_id, body.model_dump())
    except KeyError:
        raise ApiError(404, "找不到这个片段", "not_found")
    return {k: v for k, v in it.items() if k != "embedding"} | {"summary": dataset.summary(vid)}


@app.get("/v1/voices/{vid}/dataset/items/{item_id}/audio", dependencies=[Depends(auth)])
async def dataset_item_audio(vid: str, item_id: str):
    p = dataset.ddir(vid) / "clips" / f"{item_id}.wav"
    if not p.exists() or not p.resolve().is_relative_to(dataset.ddir(vid).resolve()):
        raise ApiError(404, "找不到音频", "not_found")
    return FileResponse(p, media_type="audio/wav")


class BulkBody(BaseModel):
    action: str = Field(description="delete | restore | filter")
    ids: list[str] | None = None
    min_score: int | None = None
    min_similarity: float | None = None


@app.post("/v1/voices/{vid}/dataset/bulk", dependencies=[Depends(auth)])
async def dataset_bulk(vid: str, body: BulkBody):
    if body.action not in ("delete", "restore", "filter"):
        raise ApiError(400, "action 只能是 delete / restore / filter")
    return await asyncio.to_thread(dataset.bulk, vid, body.ids, body.action, body.min_score, body.min_similarity)


class TargetBody(BaseModel):
    item_ids: list[str] | None = None
    use_ref: bool = False


@app.post("/v1/voices/{vid}/dataset/target", dependencies=[Depends(auth)])
async def dataset_target(vid: str, body: TargetBody):
    _voice_or_404(vid)
    data = await asyncio.to_thread(dataset.set_target, vid, body.item_ids, body.use_ref)
    return {"summary": dataset.summary(vid, data)}


@app.post("/v1/voices/{vid}/dataset/export", dependencies=[Depends(auth)])
async def dataset_export(vid: str):
    _voice_or_404(vid)
    return await asyncio.to_thread(dataset.export, vid)


@app.delete("/v1/voices/{vid}/dataset", dependencies=[Depends(auth)])
async def dataset_clear(vid: str):
    _voice_or_404(vid)
    await asyncio.to_thread(dataset.clear, vid)
    return {"cleared": True}
