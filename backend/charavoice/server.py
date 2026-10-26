"""CharaVoice HTTP / WebSocket API (FastAPI), bound to 127.0.0.1 only.

Authentication: every /v1 route needs the token from data/runtime.json (new on every start)
or the long-lived agent token (data/agent_token, shown on the Integration page), sent as
`Authorization: Bearer <token>` (works as an OpenAI api_key), the `X-CharaVoice-Token`
header, or a `token` query parameter (for <img>/WebSocket). MCP is also served over
Streamable HTTP at /mcp with the same tokens.
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

from . import APP_NAME, __version__, agents, asr, audio_util, config, speech, voices
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
    from . import natural

    asyncio.create_task(asyncio.to_thread(natural.prewarm))
    from . import simple as _simple

    asyncio.create_task(asyncio.to_thread(_simple.gc_orphans))
    async with mcp_http.run():
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
    if not given:
        return False
    expected = [RUNTIME.get("token", ""), RUNTIME.get("agent_token", "")]
    return any(e and hmac.compare_digest(given, e) for e in expected)


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
def _check_base(base: str) -> None:
    if base != "omnivoice":
        engine.base_weights(base)


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
        "refs": [{**r, "url": f"/v1/voices/{v['id']}/refs/{r['id']}/audio"} for r in v.get("refs") or []],
        "emotions": voices.emotions_of(v),
        "consent": v.get("consent"),
        "params": v.get("params"),
        "tweak": v.get("tweak") or {"pitch": 0.0, "formant": 0.0, "brightness": 0.0, "breath": 0.0},
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
    design_id: str = Form("", description="用 /v1/voices/design 生成的候选作为参考音频（consent 须为 designed）"),
):
    if consent not in voices.CONSENT_KINDS:
        raise ApiError(400, "新建声音前必须选择“本人声音”(self) 或“已获授权的声音”(authorized)。", "consent_required")
    if (consent == "designed") != bool(design_id):
        raise ApiError(400, "文字设计的声音（designed）必须带 design_id，且只有它可以用 designed。", "consent_required")
    if not consent_ack:
        raise ApiError(400, voices.CONSENT_NOTICE + "确认后请把 consent_ack 设为 true。", "consent_required")
    if design_id:
        from . import design

        src = design.candidate_path(design_id)
        base = base or "omnivoice"
    if base:
        _check_base(base)
    v = voices.create(name, consent, language, base)
    try:
        if design_id:
            rel = "ref/ref.wav"
            dur = await asyncio.to_thread(audio_util.to_wav_file, src.read_bytes(), voices.folder(v["id"]) / rel, 32000)
            v = voices.set_ref(v["id"], rel, ref_text.strip() or design.SAMPLE.get(language, ""), language, dur)
        elif ref_audio is not None:
            v = await _save_ref(v, ref_audio, ref_text, ref_lang)
    except Exception:
        voices.delete(v["id"])
        raise
    return _voice_public(v)


class DesignBody(BaseModel):
    language: str = Field("zh", description="zh / en")
    gender: str | None = Field(None, description="male / female")
    age: str | None = Field(None, description="child / teenager / young adult / middle-aged / elderly")
    pitch: str | None = Field(None, description="very low pitch / low pitch / moderate pitch / high pitch / very high pitch")
    whisper: bool = False
    accent: str | None = Field(None, description="英文口音，如 british accent")
    text: str | None = Field(None, description="试听用的句子；省略用默认句")
    n: int = Field(3, ge=1, le=5)


@app.post("/v1/voices/design", dependencies=[Depends(auth)])
async def design_voice(body: DesignBody):
    """Generate n candidate voices from a description (OmniVoice); pick one and create the voice with its id."""
    from . import design

    ins = design.instruct(body.gender, body.age, body.pitch, body.whisper, body.accent, body.language)
    return {"instruct": ins, "candidates": await design.candidates(ins, body.language, body.text, body.n)}


@app.get("/v1/voices/design/{cid}/audio", dependencies=[Depends(auth)])
async def design_audio(cid: str):
    from . import design

    return FileResponse(design.candidate_path(cid), media_type="audio/wav")


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
    tweak: dict[str, float] | None = None


@app.patch("/v1/voices/{vid}", dependencies=[Depends(auth)])
async def patch_voice(vid: str, body: VoicePatch):
    try:
        if body.base:
            _check_base(body.base)
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
    streaming_mode: int | None = Field(default=None, ge=0, le=3, description="0 整段 / 1 逐句 / 2 流式 / 3 极速流式；省略按设置")
    emotion: str | None = Field(default=None, description="neutral / happy / angry / sad / surprised / fearful / gentle；用声音里带该标签的参考音频")
    agent: str | None = Field(default=None, description="调用方名字；省略 voice 时用这个 Agent 绑定的声音（也可用 X-CharaVoice-Agent 头）")
    pitch: float | None = Field(default=None, ge=-6, le=6, description="音高（半音），保持音色")
    formant: float | None = Field(default=None, ge=-20, le=20, description="共振峰 / 年龄感（%），正值更年轻、偏女声")
    brightness: float | None = Field(default=None, ge=-6, le=6, description="明亮 / 温暖（dB）")
    breath: float | None = Field(default=None, ge=0, le=1, description="气声 0-1")
    preset: str | None = Field(default=None, description="声线预设：sweet / deep / younger / mature")
    naturalness: bool | str | dict | None = Field(default=None, description="自然度：true/\"on\" 自然模式，false/\"off\" 原始输出，或开关字典；省略按设置")


def _tweak(body) -> dict:
    return {k: getattr(body, k) for k in ("pitch", "formant", "brightness", "breath") if getattr(body, k) is not None}


def _bound(request: Request, agent: str | None, via: str) -> tuple[str | None, str | None]:
    """(voice id, emotion) bound to the calling agent, if it named itself and has a binding."""
    a = agents.touch(agent or request.headers.get("x-charavoice-agent"), via)
    if not a:
        return None, None
    vid = None
    if a.get("voice"):
        try:
            vid = voices.find(a["voice"])["id"]
        except LookupError:  # the bound voice was deleted: fall back to the default voice
            vid = None
    return vid, a.get("emotion")


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
async def audio_speech(body: SpeechRequest, request: Request):
    """OpenAI-compatible TTS. `model` or `voice` = CharaVoice voice id or name."""
    bound_voice, bound_emotion = _bound(request, body.agent, "http")
    vid = _pick_voice(body.voice, body.model) or bound_voice
    body.emotion = body.emotion or bound_emotion
    streaming = body.stream or body.stream_format == "audio"
    fmt = (body.response_format or "wav").lower()
    if streaming and fmt not in ("wav", "pcm"):
        raise ApiError(400, "流式输出只支持 response_format=wav 或 pcm。")
    job = speech.start(body.input, vid, body.language, play=body.play, speed=body.speed, live=streaming, streaming_mode=body.streaming_mode,
                       emotion=body.emotion, tweak=_tweak(body), preset=body.preset, naturalness=body.naturalness)
    if streaming:
        from . import compliance

        async def gen():
            first = True
            finished = False
            try:
                async for sr, pcm in speech.stream(job):
                    if first and fmt == "wav":
                        yield compliance.wav_stream_header(sr)
                    first = False
                    yield pcm.astype("<i2").tobytes()
                finished = True
            finally:
                if not finished:  # client disconnected mid-stream: free the engine for the next request
                    job.abandoned = True
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
    low_latency: bool = Field(default=False, description="极速流式：首包约 0.2 秒，音质略低；默认逐句流式（首包约 0.5-0.8 秒）")
    emotion: str | None = Field(default=None, description="neutral / happy / angry / sad / surprised / fearful / gentle")
    agent: str | None = Field(default=None, description="调用方名字；省略 voice 时用这个 Agent 绑定的声音")
    via: str = Field(default="http", description="接入方式（mcp / cli / http），只用于界面显示")
    pitch: float | None = Field(default=None, ge=-6, le=6, description="音高（半音），保持音色")
    formant: float | None = Field(default=None, ge=-20, le=20, description="共振峰 / 年龄感（%），正值更年轻、偏女声")
    brightness: float | None = Field(default=None, ge=-6, le=6, description="明亮 / 温暖（dB）")
    breath: float | None = Field(default=None, ge=0, le=1, description="气声 0-1")
    preset: str | None = Field(default=None, description="声线预设：sweet / deep / younger / mature")
    naturalness: bool | str | dict | None = Field(default=None, description="自然度：true/\"on\" 自然模式，false/\"off\" 原始输出，或开关字典；省略按设置")


@app.post("/v1/speak", dependencies=[Depends(auth)])
async def speak(body: SpeakBody, request: Request):
    """Convenience endpoint for agents/CLI: synthesize and play locally, return JSON."""
    bound_voice, bound_emotion = _bound(request, body.agent, body.via if body.via in ("mcp", "cli", "http") else "http")
    vid = _pick_voice(body.voice) or bound_voice
    body.emotion = body.emotion or bound_emotion
    t0 = time.monotonic()
    job = speech.start(body.text, vid, body.language, play=True, speed=body.speed, streaming_mode=3 if body.low_latency else None,
                       emotion=body.emotion, tweak=_tweak(body), preset=body.preset, naturalness=body.naturalness)
    utt = job.utterance
    assert utt is not None
    started = await asyncio.to_thread(_wait_any, utt, 120.0)
    latency_ms = int((time.monotonic() - t0) * 1000)  # request -> first audio handed to the device
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
        "emotion": body.emotion or "neutral",
        "started": started,
        "finished": utt.done.is_set(),
        "audio_seconds": seconds if job.done.is_set() else None,
        "latency_ms": latency_ms if started else None,
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


LISTEN_EVENTS = {"speech_start", "partial", "speech_end", "transcript", "listen_start", "listen_stop", "listen_error"}


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
    emotion: str | None = None
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


@app.post("/v1/voices/{vid}/dataset/sources", dependencies=[Depends(auth)])
async def dataset_add_source(vid: str, file: UploadFile = File(...)):
    """Upload an audio/video file; it waits in the trim editor until processed."""
    _voice_or_404(vid)
    raw = await file.read()
    return await asyncio.to_thread(dataset.add_source, vid, file.filename or "upload", raw)


@app.get("/v1/voices/{vid}/dataset/sources/{sid}/peaks", dependencies=[Depends(auth)])
async def dataset_peaks(vid: str, sid: str, per_second: int = 100):
    if not dataset.work_wav(vid, sid).exists():
        raise ApiError(404, "找不到这个素材", "not_found")
    return await asyncio.to_thread(dataset.peaks, vid, sid, per_second)


@app.get("/v1/voices/{vid}/dataset/sources/{sid}/audio", dependencies=[Depends(auth)])
async def dataset_source_audio(vid: str, sid: str):
    p = dataset.work_wav(vid, sid)
    if not p.exists():
        raise ApiError(404, "找不到这个素材", "not_found")
    return FileResponse(p, media_type="audio/wav")


class ProcessBody(BaseModel):
    ranges: list[list[float]] | None = Field(default=None, description="保留的时间段 [[开始秒, 结束秒], ...]；省略 = 整段")
    separate: bool = True


@app.post("/v1/voices/{vid}/dataset/sources/{sid}/process", dependencies=[Depends(auth)])
async def dataset_process(vid: str, sid: str, body: ProcessBody):
    try:
        job = dataset.start_process(vid, sid, body.ranges, body.separate)
    except KeyError:
        raise ApiError(404, "找不到这个素材", "not_found")
    return job.public()


@app.delete("/v1/voices/{vid}/dataset/sources/{sid}", dependencies=[Depends(auth)])
async def dataset_remove_source(vid: str, sid: str):
    try:
        await asyncio.to_thread(dataset.remove_source, vid, sid)
    except KeyError:
        raise ApiError(404, "找不到这个素材", "not_found")
    return {"removed": sid}


# ---------------------------------------------------------------- loopback recording
from .recorder import recorder  # noqa: E402
from . import recorder as recorder_mod  # noqa: E402


@app.get("/v1/record/devices", dependencies=[Depends(auth)])
async def record_devices():
    try:
        return {"devices": await asyncio.to_thread(recorder_mod.devices)}
    except Exception as e:
        raise ApiError(500, f"无法列出可录制的设备：{e}", "record_failed")


class RecordStart(BaseModel):
    voice: str
    device: int | None = None
    consent_ack: bool = Field(False, description="确认只录制本人或已获授权的声音")


@app.post("/v1/record/start", dependencies=[Depends(auth)])
async def record_start(body: RecordStart):
    v = _voice_or_404(body.voice)
    if not body.consent_ack:
        raise ApiError(400, "只录制本人或已获授权的声音。请确认后再开始录制（consent_ack=true）。", "consent_required")
    if not v.get("consent"):
        raise ApiError(400, "这个声音没有授权记录，不能录制素材。", "consent_required")
    rec = await asyncio.to_thread(recorder.start, body.voice, dataset.ddir(body.voice) / "sources", body.device)
    return {"id": rec.id, "device": rec.device}


@app.post("/v1/record/stop", dependencies=[Depends(auth)])
async def record_stop():
    rec = recorder.stop()
    if not rec:
        raise ApiError(400, "当前没有在录音。")
    await asyncio.to_thread(rec.stopped.wait, 10)
    if rec.seconds < 1.0:
        rec.path.unlink(missing_ok=True)
        raise ApiError(400, rec.error or "录音太短（不到 1 秒），已丢弃。")
    src = await asyncio.to_thread(dataset.add_source, rec.voice, f"录音 {time.strftime('%H:%M:%S', time.localtime(rec.started))}", None, rec.path, "loopback")
    return {"recording": {"id": rec.id, "seconds": round(rec.seconds, 2), "error": rec.error}, "source": src}


@app.get("/v1/record/status", dependencies=[Depends(auth)])
async def record_status():
    rec = recorder.current
    if not rec:
        return {"recording": False}
    return {"recording": not rec.stopped.is_set(), "id": rec.id, "voice": rec.voice, "seconds": round(rec.seconds, 2), "device": rec.device, "error": rec.error}


# ---------------------------------------------------------------- training (M4)
from . import training  # noqa: E402


@app.get("/v1/voices/{vid}/train/precheck", dependencies=[Depends(auth)])
async def train_precheck(vid: str):
    _voice_or_404(vid)
    return await asyncio.to_thread(training.precheck, vid)


class TrainBody(BaseModel):
    s2_epochs: int | None = None
    s1_epochs: int | None = None
    s2_batch: int | None = None
    s1_batch: int | None = None
    fresh: bool = Field(False, description="丢弃上次的训练进度，从头开始")
    base: str | None = Field(None, description="微调哪个底模：v2ProPlus（默认）/ v2Pro；省略按声音的基础模型")


@app.post("/v1/voices/{vid}/train/start", dependencies=[Depends(auth)])
async def train_start(vid: str, body: TrainBody):
    _voice_or_404(vid)
    job = await asyncio.to_thread(training.start, vid, body.model_dump())
    return job.public()


@app.post("/v1/voices/{vid}/train/cancel", dependencies=[Depends(auth)])
async def train_cancel(vid: str):
    return {"cancelled": await asyncio.to_thread(training.cancel, vid)}


@app.get("/v1/voices/{vid}/train", dependencies=[Depends(auth)])
async def train_status(vid: str, log_lines: int = 200):
    _voice_or_404(vid)
    job = training.jobs.get(vid)
    return {
        "job": job.public() if job else None,
        "state": training.load_state(vid),
        "log": training.logs.get(vid, [])[-max(0, min(log_lines, 2000)) :],
        "stages": [{"key": k, "label": l} for k, l in training.STAGES],
        "samples": sorted(p.name for p in (training.tdir(vid) / "samples").glob("*.wav")) if (training.tdir(vid) / "samples").exists() else [],
    }


@app.get("/v1/voices/{vid}/train/samples/{name}", dependencies=[Depends(auth)])
async def train_sample(vid: str, name: str):
    p = training.sample_path(vid, name)
    if not p:
        raise ApiError(404, "没有这个试听文件", "not_found")
    return FileResponse(p, media_type="audio/wav", headers={"Cache-Control": "no-cache"})


@app.post("/v1/audio/watermark/detect", dependencies=[Depends(auth)])
async def watermark_detect(file: UploadFile = File(...)):
    """Check an audio file for the AudioSeal watermark CharaVoice embeds in API output."""
    from . import watermark

    samples = await asyncio.to_thread(audio_util.decode, await file.read(), 16000)
    r = await asyncio.to_thread(watermark.detect, samples, 16000)
    return r | {"note": "probability > 0.5 表示检测到 AudioSeal 水印；charavoice=true 表示 16 位标识也匹配（有损压缩后标识可能读不出）"}



# ---------------------------------------------------------------- emotion references (P2)
@app.get("/v1/emotions", dependencies=[Depends(auth)])
async def list_emotions():
    return {"emotions": [{"id": k, "label": v} for k, v in voices.EMOTIONS.items()]}


async def _store_emotion_ref(v: dict, raw: bytes, text: str, lang: str, emotion: str, source: str) -> dict:
    rel = f"ref/{emotion}-{secrets.token_hex(3)}.wav"
    dst = voices.folder(v["id"]) / rel
    dur = await asyncio.to_thread(audio_util.to_wav_file, raw, dst, 32000)
    if dur < 3 or dur > 10:
        dst.unlink(missing_ok=True)
        raise ApiError(400, f"参考音频时长 {dur:.1f} 秒，需要在 3-10 秒之间。")
    if not text.strip() or not lang:
        r = await asyncio.to_thread(asr.transcribe, await asyncio.to_thread(audio_util.decode, dst, 16000))
        text = text.strip() or r["text"]
        lang = lang or (r["language"] if r["language"] in voices.LANGS else v.get("language", "zh"))
    return voices.add_emotion_ref(v["id"], rel, text, speech.norm_lang(lang, text), dur, emotion, source)


@app.post("/v1/voices/{vid}/refs", dependencies=[Depends(auth)])
async def add_ref(vid: str, file: UploadFile = File(...), emotion: str = Form(...), text: str = Form(""), lang: str = Form("")):
    """Add a reference clip tagged with an emotion (3-10 s)."""
    v = _voice_or_404(vid)
    if emotion not in voices.EMOTIONS:
        raise ApiError(400, f"未知的情绪标签：{emotion}")
    return _voice_public(await _store_emotion_ref(v, await file.read(), text, lang, emotion, "upload"))


class RefFromClip(BaseModel):
    item_id: str
    emotion: str


@app.post("/v1/voices/{vid}/refs/from-clip", dependencies=[Depends(auth)])
async def add_ref_from_clip(vid: str, body: RefFromClip):
    """Use a reviewed training clip as an emotion reference."""
    v = _voice_or_404(vid)
    if body.emotion not in voices.EMOTIONS:
        raise ApiError(400, f"未知的情绪标签：{body.emotion}")
    data = await asyncio.to_thread(dataset.load, vid)
    it = next((i for i in data["items"] if i["id"] == body.item_id), None)
    if not it:
        raise ApiError(404, "找不到这个片段", "not_found")
    raw = (dataset.ddir(vid) / "clips" / f"{it['id']}.wav").read_bytes()
    return _voice_public(await _store_emotion_ref(v, raw, it["text"], it["lang"], body.emotion, f"clip:{it['id']}"))


@app.delete("/v1/voices/{vid}/refs/{rid}", dependencies=[Depends(auth)])
async def delete_ref(vid: str, rid: str):
    _voice_or_404(vid)
    try:
        return _voice_public(voices.remove_emotion_ref(vid, rid))
    except KeyError:
        raise ApiError(404, "找不到这段参考音频", "not_found")


@app.get("/v1/voices/{vid}/refs/{rid}/audio", dependencies=[Depends(auth)])
async def ref_audio(vid: str, rid: str):
    v = _voice_or_404(vid)
    r = next((x for x in v.get("refs") or [] if x["id"] == rid), None)
    if not r:
        raise ApiError(404, "找不到这段参考音频", "not_found")
    return FileResponse(voices.abspath(v, r["audio"]), media_type="audio/wav")



# ---------------------------------------------------------------- engines (P3)
from .engine import ENGINES  # noqa: E402


@app.get("/v1/engines", dependencies=[Depends(auth)])
async def engines_list():
    return {"engines": [e.info() | {"label": e.label, "installed": e.installed()} for e in ENGINES.values()]}


@app.post("/v1/engines/{name}/start", dependencies=[Depends(auth)])
async def engines_start(name: str):
    e = ENGINES.get(name)
    if not e:
        raise ApiError(404, f"没有这个引擎：{name}", "not_found")
    asyncio.create_task(e.start())
    await asyncio.sleep(0.1)
    return e.info()


@app.post("/v1/engines/{name}/stop", dependencies=[Depends(auth)])
async def engines_stop(name: str):
    e = ENGINES.get(name)
    if not e:
        raise ApiError(404, f"没有这个引擎：{name}", "not_found")
    await e.stop()
    return e.info()


# ---------------------------------------------------------------- capability discovery (P4)
@app.get("/.well-known/charavoice.json")
async def well_known():
    """Unauthenticated discovery: what this local service can do and where (no voice data, no token)."""
    from .engine import ENGINES

    return {
        "name": APP_NAME,
        "version": __version__,
        "description": "Local character voice service: text-to-speech in trained or zero-shot character voices, and speech-to-text.",
        "auth": {"type": "bearer", "token_file": "data/runtime.json", "header": "Authorization: Bearer <token>"},
        "languages": {"speak": ["zh", "en", "ja"], "listen": ["zh", "en", "ja", "ko", "yue"]},
        "emotions": list(voices.EMOTIONS),
        "engines": [{"name": e.name, "label": e.label, "installed": e.installed(), "state": e.state} for e in ENGINES.values()],
        "endpoints": {
            "speech_openai": {"method": "POST", "path": "/v1/audio/speech", "stream": True},
            "transcriptions_openai": {"method": "POST", "path": "/v1/audio/transcriptions"},
            "speak": {"method": "POST", "path": "/v1/speak"},
            "listen_once": {"method": "POST", "path": "/v1/listen"},
            "listen_stream": {"method": "WS", "path": "/v1/listen", "events": ["speech_start", "partial", "speech_end", "transcript"]},
            "events": {"method": "WS", "path": "/v1/events"},
            "voices": {"method": "GET", "path": "/v1/voices"},
            "openapi": {"method": "GET", "path": "/openapi.json"},
        },
        "mcp": {"stdio": "charavoice-mcp", "streamable_http": "/mcp", "tools": ["speak", "list_voices", "listen", "transcribe"]},
        "naturalness": {"field": "naturalness", "values": ["on", "off", "{pauses, breaths, dynamics, loudness, room}"]},
        "voice_tweak": {"fields": ["pitch", "formant", "brightness", "breath", "preset"], "info": "/v1/voicefx"},
        "agent_binding": {"header": "X-CharaVoice-Agent", "body_field": "agent", "mcp": "client name from initialize, or CHARAVOICE_AGENT",
                          "note": "omit voice to speak with the voice the user bound to this agent"},
    }


# ---------------------------------------------------------------- global dictation (P4)
from .dictation import dictation  # noqa: E402


@app.post("/v1/dictation/toggle", dependencies=[Depends(auth)])
async def dictation_toggle(type_into_focus: bool = True):
    """Start listening for one turn and type the result into the focused window; call again to cancel."""
    started = dictation.toggle(type_into_focus)
    return {"active": started}


# ---------------------------------------------------------------- voice tweaks
@app.get("/v1/voicefx", dependencies=[Depends(auth)])
async def voicefx_info():
    from . import voicefx

    return {"limits": voicefx.LIMITS, "presets": voicefx.PRESETS, "labels": voicefx.PRESET_LABEL}


# ---------------------------------------------------------------- agents (P5)
class AgentPatch(BaseModel):
    voice: str | None = None
    emotion: str | None = None


@app.get("/v1/agents", dependencies=[Depends(auth)])
async def list_agents():
    return {"agents": agents.all_agents()}


@app.patch("/v1/agents/{name}", dependencies=[Depends(auth)])
async def patch_agent(name: str, body: AgentPatch):
    patch = body.model_dump(exclude_unset=True)
    if patch.get("voice"):
        patch["voice"] = voices.find(patch["voice"])["id"]
    if patch.get("emotion") and patch["emotion"] not in voices.EMOTIONS:
        raise ApiError(400, f"未知情绪：{patch['emotion']}")
    return agents.update(name, patch)


@app.delete("/v1/agents/{name}", dependencies=[Depends(auth)])
async def delete_agent(name: str):
    agents.remove(name)
    return {"deleted": True}


@app.get("/v1/integration", dependencies=[Depends(auth)])
async def integration():
    base = RUNTIME.get("base_url", "http://127.0.0.1:9870")
    return {"base_url": base, "mcp_url": base + "/mcp", "agent_token": RUNTIME.get("agent_token", "")}


@app.post("/v1/integration/rotate-token", dependencies=[Depends(auth)])
async def rotate_agent_token():
    RUNTIME["agent_token"] = config.agent_token(rotate=True)
    return await integration()


# ---------------------------------------------------------------- MCP over Streamable HTTP (P5)
class _McpHttp:
    """Serve the MCP tools at /mcp on this backend (loopback only, same tokens as the API)."""

    def __init__(self) -> None:
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from mcp.server.transport_security import TransportSecuritySettings

        from .mcp_server import server as mcp_server

        self.manager = StreamableHTTPSessionManager(
            app=mcp_server._lowlevel_server,
            security_settings=TransportSecuritySettings(
                allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
                allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
            ),
        )

    def run(self):
        return self.manager.run()

    async def __call__(self, scope, receive, send):
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        h = headers.get("authorization", "")
        bearer = h[7:].strip() if h.lower().startswith("bearer ") else None
        if not (_token_ok(bearer) or _token_ok(headers.get("x-charavoice-token"))):
            resp = _err(401, "缺少或错误的访问口令：在 MCP 配置里加上 Authorization: Bearer <Agent 口令>（见“接入 Agent”页）。", "unauthorized")
            await resp(scope, receive, send)
            return
        await self.manager.handle_request(scope, receive, send)


mcp_http = _McpHttp()
app.router.add_route("/mcp", mcp_http, methods=["GET", "POST", "DELETE"], include_in_schema=False)


# ---------------------------------------------------------------- breath library (naturalness)
@app.get("/v1/voices/{vid}/breaths", dependencies=[Depends(auth)])
async def get_breaths(vid: str):
    from . import breaths

    voices.get(vid)
    return {"breaths": breaths.load(vid)}


@app.post("/v1/voices/{vid}/breaths/build", dependencies=[Depends(auth)])
async def build_breaths(vid: str):
    """Scan this voice's material (imported sources and the reference) for inhalations."""
    from . import breaths, dataset

    v = voices.get(vid)
    d = dataset.ddir(vid)
    files = sorted((d / "sources").glob("*")) if (d / "sources").exists() else []
    files += [Path(voices.abspath(v, r["audio"])) for r in v.get("refs") or []]
    if v.get("ref"):
        files.append(Path(voices.abspath(v, v["ref"]["audio"])))
    idx = await asyncio.to_thread(breaths.build, vid, files)
    return {"breaths": idx, "scanned": len(files)}


# ---------------------------------------------------------------- model manager (P6)
@app.get("/v1/assets", dependencies=[Depends(auth)])
async def list_assets():
    from . import assets

    return {"assets": assets.listing(), "missing_required": assets.missing_required(), "home": str(config.ROOT)}


@app.post("/v1/assets/{aid}/install", dependencies=[Depends(auth)])
async def install_asset(aid: str):
    from . import assets

    return {"job": assets.start(aid).state()}


@app.post("/v1/assets/{aid}/cancel", dependencies=[Depends(auth)])
async def cancel_asset(aid: str):
    from . import assets

    return {"cancelled": assets.cancel(aid)}


@app.delete("/v1/assets/{aid}", dependencies=[Depends(auth)])
async def delete_asset(aid: str):
    from . import assets
    from .engine import engine_for

    if aid in ("gpt-sovits", "omnivoice"):
        await engine_for(aid).stop()
    await asyncio.to_thread(assets.remove, aid)
    return {"deleted": aid}


# ---------------------------------------------------------------- simple mode (material folders, Japanese)
from . import simple  # noqa: E402


def _folder_or_404(vid: str) -> dict:
    v = _voice_or_404(vid)
    if not simple.is_folder(v):
        raise ApiError(404, "不是素材文件夹。", "not_found")
    return v


@app.get("/v1/simple", dependencies=[Depends(auth)])
async def simple_state():
    return {"consent_at": simple.consent(), "folders": await asyncio.to_thread(simple.folders), "recording": simple.recording_now(),
            "settings": config.load_settings()["simple"]}


@app.post("/v1/simple/consent", dependencies=[Depends(auth)])
async def simple_consent():
    return {"consent_at": simple.set_consent()}


class FolderBody(BaseModel):
    name: str


@app.post("/v1/simple/folders", dependencies=[Depends(auth)])
async def simple_create_folder(body: FolderBody):
    return simple.create_folder(body.name)


@app.get("/v1/simple/folders/{vid}", dependencies=[Depends(auth)])
async def simple_folder(vid: str):
    _folder_or_404(vid)
    return simple.folder_detail(vid)


@app.patch("/v1/simple/folders/{vid}", dependencies=[Depends(auth)])
async def simple_rename_folder(vid: str, body: FolderBody):
    _folder_or_404(vid)
    return simple.summary(voices.update(vid, {"name": body.name}))


@app.delete("/v1/simple/folders/{vid}", dependencies=[Depends(auth)])
async def simple_delete_folder(vid: str):
    _folder_or_404(vid)
    if (j := simple.jobs.get(vid)) and j.state == "running":
        raise ApiError(409, "这个文件夹正在学习，请先取消。", "busy")
    await asyncio.to_thread(simple.delete_folder, vid)
    return {"deleted": vid}


class SimpleRecord(BaseModel):
    device: int | None = None


@app.post("/v1/simple/record/toggle", dependencies=[Depends(auth)])
async def simple_record_toggle(body: SimpleRecord | None = None):
    try:
        return await asyncio.to_thread(simple.record_toggle, body.device if body else None)
    except PermissionError:
        raise ApiError(403, "首次录制前请确认只录制本人或已获授权的声音。", "consent_required")


@app.get("/v1/simple/recordings/{rid}/peaks", dependencies=[Depends(auth)])
async def simple_rec_peaks(rid: str, per_second: int = 100):
    return await asyncio.to_thread(simple.rec_peaks, rid, per_second)


@app.get("/v1/simple/recordings/{rid}/audio", dependencies=[Depends(auth)])
async def simple_rec_audio(rid: str):
    return FileResponse(simple.rec_file(rid), media_type="audio/flac")


@app.get("/v1/simple/recordings/{rid}/analyze", dependencies=[Depends(auth)])
async def simple_rec_analyze(rid: str, folder: str | None = None):
    return await asyncio.to_thread(simple.analyze, rid, folder)


@app.delete("/v1/simple/recordings/{rid}", dependencies=[Depends(auth)])
async def simple_rec_discard(rid: str):
    return {"deleted": await asyncio.to_thread(simple.discard_recording, rid)}


class ClipSave(BaseModel):
    rec: str
    ranges: list[list[float]]
    name: str | None = None
    tag: str | None = None
    replace: str | None = Field(default=None, description="重新裁剪：替换这个片段")


@app.post("/v1/simple/folders/{vid}/clips", dependencies=[Depends(auth)])
async def simple_save_clip(vid: str, body: ClipSave):
    _folder_or_404(vid)
    return await asyncio.to_thread(simple.save_clip, vid, body.rec, body.ranges, body.name, body.tag, body.replace)


@app.post("/v1/simple/folders/{vid}/import", dependencies=[Depends(auth)])
async def simple_import(vid: str, files: list[UploadFile] = File(...)):
    _folder_or_404(vid)
    out = []
    for f in files:
        out.append(await asyncio.to_thread(simple.import_file, vid, f.filename or "audio", await f.read()))
    return {"clips": out}


class ImportPaths(BaseModel):
    paths: list[str]


@app.post("/v1/simple/folders/{vid}/import-paths", dependencies=[Depends(auth)])
async def simple_import_paths(vid: str, body: ImportPaths):
    """Files dropped onto the window (the desktop shell hands over local paths)."""
    _folder_or_404(vid)
    out, errors = [], []
    for p in body.paths:
        path = Path(p)
        if not path.is_file():
            errors.append(f"{path.name}：不是文件")
            continue
        try:
            out.append(await asyncio.to_thread(simple.import_file, vid, path.name, None, path))
        except ValueError as e:
            errors.append(str(e))
    return {"clips": out, "errors": errors}


@app.get("/v1/simple/folders/{vid}/clips/{cid}/audio", dependencies=[Depends(auth)])
async def simple_clip_audio(vid: str, cid: str):
    _folder_or_404(vid)
    return FileResponse(simple.clip_file(vid, cid), media_type="audio/wav")


class ClipPatch(BaseModel):
    name: str | None = None
    tag: str | None = None


@app.patch("/v1/simple/folders/{vid}/clips/{cid}", dependencies=[Depends(auth)])
async def simple_patch_clip(vid: str, cid: str, body: ClipPatch):
    _folder_or_404(vid)
    return simple.patch_clip(vid, cid, body.name, body.tag)


class ClipIds(BaseModel):
    ids: list[str]
    to: str | None = None


@app.post("/v1/simple/folders/{vid}/clips/delete", dependencies=[Depends(auth)])
async def simple_delete_clips(vid: str, body: ClipIds):
    _folder_or_404(vid)
    return {"deleted": simple.delete_clips(vid, body.ids)}


@app.post("/v1/simple/folders/{vid}/clips/move", dependencies=[Depends(auth)])
async def simple_move_clips(vid: str, body: ClipIds):
    _folder_or_404(vid)
    if not body.to:
        raise ApiError(400, "请选择目标文件夹。")
    _folder_or_404(body.to)
    return {"moved": simple.move_clips(vid, body.to, body.ids)}


class CloneBody(BaseModel):
    train: bool = Field(True, description="素材够 1 分钟时训练 v2ProPlus；false 只做零样本")
    separate: bool | None = Field(None, description="人声分离；省略按设置")


@app.post("/v1/simple/folders/{vid}/clone", dependencies=[Depends(auth)])
async def simple_clone(vid: str, body: CloneBody):
    _folder_or_404(vid)
    sep = body.separate if body.separate is not None else config.load_settings()["simple"]["separate"]
    return simple.start_clone(vid, body.train, sep).public()


@app.post("/v1/simple/folders/{vid}/clone/cancel", dependencies=[Depends(auth)])
async def simple_clone_cancel(vid: str):
    return {"cancelled": simple.cancel_clone(vid)}


class ExportBody(BaseModel):
    text: str
    format: str = "wav"


@app.post("/v1/simple/folders/{vid}/export", dependencies=[Depends(auth)])
async def simple_export(vid: str, body: ExportBody):
    _folder_or_404(vid)
    try:
        return await simple.export(vid, body.text, body.format)
    except RuntimeError as e:
        raise ApiError(502, str(e), "synthesis_failed")


@app.post("/v1/simple/folders/{vid}/open", dependencies=[Depends(auth)])
async def simple_open_folder(vid: str):
    """Show the folder's clips in Explorer."""
    _folder_or_404(vid)
    d = simple.clip_dir(vid)
    d.mkdir(parents=True, exist_ok=True)
    os.startfile(str(d))  # type: ignore[attr-defined]  # Windows only, like the rest of the app
    return {"opened": str(d)}
