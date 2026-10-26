"""Text-to-speech orchestration: voice lookup, language choice, local playback, tee to clients."""

from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass, field, replace
from typing import AsyncIterator

import numpy as np

from . import breaths, config, natural, voicefx, voices
from .engine import EngineUnavailable, SynthRequest, engine, engine_for
from .player import Utterance, player

_KANA = re.compile(r"[぀-ヿㇰ-ㇿ]")
_HAN = re.compile(r"[一-鿿㐀-䶿]")
_HANGUL = re.compile(r"[가-힯]")
LANG_ALIASES = {
    "zh": "zh", "zh-cn": "zh", "cn": "zh", "chinese": "zh", "中文": "zh",
    "en": "en", "en-us": "en", "english": "en", "英文": "en", "英语": "en",
    "ja": "ja", "jp": "ja", "japanese": "ja", "日语": "ja", "日文": "ja",
    "ko": "ko", "yue": "yue", "auto": "auto",
}


def detect_lang(text: str) -> str:
    """Pick GPT-SoVITS text_lang. Kana means Japanese even when kanji are present."""
    if _KANA.search(text):
        return "ja"
    if _HANGUL.search(text):
        return "ko"
    if _HAN.search(text):
        return "zh"
    return "en"


def norm_lang(lang: str | None, text: str) -> str:
    if not lang:
        return detect_lang(text)
    key = lang.strip().lower()
    if key not in LANG_ALIASES:
        raise ValueError(f"不支持的语种：{lang}（可用 zh / en / ja / auto）")
    v = LANG_ALIASES[key]
    return detect_lang(text) if v == "auto" else v


def build_request(voice: dict, text: str, language: str | None, speed: float | None = None,
                  streaming_mode: int | None = None, emotion: str | None = None) -> SynthRequest:
    if emotion and emotion not in voices.EMOTIONS:
        raise ValueError(f"未知的情绪：{emotion}（可用：{', '.join(voices.EMOTIONS)}）")
    ref = voices.pick_ref(voice, emotion, norm_lang(language, text))
    if not ref:
        raise LookupError(f"声音“{voice['name']}”还没有参考音频，请先在声音设置里上传一段 3-10 秒的参考音频。")
    s = config.load_settings()["engine"]
    base = voice.get("base") or s["version"]
    if base == "omnivoice":
        return SynthRequest(
            text=text, text_lang=norm_lang(language, text), ref_audio=voices.abspath(voice, ref["audio"]),
            prompt_text=ref.get("text", ""), prompt_lang=ref.get("lang") or voice.get("language") or "zh",
            gpt_weights="", sovits_weights="", speed=float(speed or (voice.get("params") or {}).get("speed") or 1.0),
            streaming_mode=int(s["streaming_mode"] if streaming_mode is None else streaming_mode), engine="omnivoice",
        )
    base_gpt, base_sovits = engine.base_weights(base)
    w = voice.get("weights") or {}
    p = voice.get("params") or {}
    return SynthRequest(
        text=text,
        text_lang=norm_lang(language, text),
        ref_audio=voices.abspath(voice, ref["audio"]),
        prompt_text=ref.get("text", ""),
        prompt_lang=ref.get("lang") or voice.get("language") or "zh",
        gpt_weights=voices.abspath(voice, w.get("gpt")) or base_gpt,
        sovits_weights=voices.abspath(voice, w.get("sovits")) or base_sovits,
        aux_refs=[voices.abspath(voice, a) for a in voice.get("aux_refs") or []],
        speed=float(speed or p.get("speed") or 1.0),
        streaming_mode=int(s["streaming_mode"] if streaming_mode is None else streaming_mode),
        top_k=int(p.get("top_k", 15)),
        top_p=float(p.get("top_p", 1.0)),
        temperature=float(p.get("temperature", 1.0)),
    )


@dataclass
class SpeakJob:
    voice: dict
    text: str
    request: SynthRequest
    play: bool
    utterance: Utterance | None = None
    sr: int = 0
    chunks: list[np.ndarray] = field(default_factory=list)
    error: str | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    live: asyncio.Queue | None = None
    tweak: voicefx.Tweak = field(default_factory=voicefx.Tweak)
    natural: dict = field(default_factory=lambda: dict(natural.OFF))
    abandoned: bool = False  # the HTTP client went away: stop synthesizing so the engine is free

    def stopped(self) -> bool:
        if self.abandoned:
            return True
        return self.utterance is not None and self.utterance.cancelled and self.live is None

    def audio(self) -> np.ndarray:
        return np.concatenate(self.chunks) if self.chunks else np.zeros(0, dtype=np.int16)


async def _emit(job: SpeakJob, sr: int, pcm: np.ndarray) -> None:
    if not len(pcm):
        return
    job.sr = sr
    job.chunks.append(pcm)
    if job.utterance is not None:
        job.utterance.feed(sr, pcm)
    if job.live is not None:
        await job.live.put((sr, pcm))


def _to_pcm(x: np.ndarray) -> np.ndarray:
    return (np.clip(x, -1, 1) * 32767).astype(np.int16)


_BREATH_CACHE: dict[tuple[str, str], np.ndarray] = {}


def _breath(job: SpeakJob, sr: int, speech_db: float, rng: random.Random) -> np.ndarray | None:
    """A breath clip from the voice's library, scaled to the speaker's breath-to-speech level."""
    lib = breaths.load(job.voice["id"])
    if not lib:
        return None
    b = rng.choice(lib)
    key = (job.voice["id"], b["file"])
    if key not in _BREATH_CACHE:
        import soundfile as sf

        x, bsr = sf.read(str(breaths.lib_dir(job.voice["id"]) / b["file"]), dtype="float32")
        _BREATH_CACHE[key] = x if bsr == sr else np.interp(np.arange(int(len(x) * sr / bsr)) * bsr / sr, np.arange(len(x)), x).astype(np.float32)
    x = _BREATH_CACHE[key]
    x = x[: int(sr * 0.35)]  # 200-350 ms is the natural length; longer clips are cut
    lvl = 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9)
    rel = float(np.clip(b.get("rel_db", -26), -34, -18)) - 3  # a little quieter than measured: restraint
    return (x * 10 ** ((speech_db + rel - lvl) / 20)).astype(np.float32)


def _level_db(x: np.ndarray, sr: int) -> float:
    blk = max(1, sr // 100)
    n = len(x) // blk * blk
    if not n:
        return -40.0
    r = np.sqrt(np.mean(x[:n].reshape(-1, blk) ** 2, 1) + 1e-12)
    return float(20 * np.log10(np.percentile(r, 95)))


async def _produce(job: SpeakJob) -> None:
    cfg = job.natural
    rng = random.Random()
    clauses = natural.plan(job.text, cfg, job.request.speed, rng) if cfg.get("pauses") else [natural.Clause(job.text, 0.0, True)]
    post: natural.Post | None = None
    sr = 0
    speech_db = -20.0

    async def out(x: np.ndarray, silent: bool = False) -> None:
        nonlocal post
        if not len(x):
            return
        if natural.any_post(cfg):
            post = post or natural.Post(sr, cfg)
            x = await asyncio.to_thread(post.process, x, silent)
        await _emit(job, sr, _to_pcm(x))

    try:
        eng = engine_for(job.request.engine)
        for i, cl in enumerate(clauses):
            if job.stopped():
                break
            req = replace(job.request, text=cl.text, text_split="cut0") if cfg.get("pauses") else job.request
            trim = natural.Trimmer(1) if cfg.get("pauses") else None
            fx: voicefx.Stream | None = None
            clause_audio: list[np.ndarray] = []

            async def push(x: np.ndarray) -> None:
                nonlocal fx
                if not len(x):
                    return
                clause_audio.append(x)
                if not job.tweak.active:
                    await out(x)
                    return
                # voice tweaks: block by block off the event loop, so the first block plays early
                fx = fx or voicefx.Stream(sr, job.tweak)
                it = fx.feed(x)
                while (blk := await asyncio.to_thread(next, it, None)) is not None:
                    await out(blk)

            stream = eng.synthesize(req)
            try:
                async for csr, pcm in stream:
                    if job.stopped():
                        break
                    if not sr:
                        sr = csr
                    x = pcm.astype(np.float32) / 32768
                    if trim is not None:
                        trim.sr = sr
                        x = trim.feed(x)
                    await push(x)
            finally:
                await stream.aclose()  # closes the engine request and releases the engine at once
            if job.stopped():
                break
            if trim is not None:
                await push(trim.flush())
            if fx is not None:
                await out(await asyncio.to_thread(fx.flush))
            if clause_audio:
                speech_db = _level_db(np.concatenate(clause_audio), sr)
            # the pause after this clause, with a breath before a long new sentence now and then
            if cl.pause > 0 and sr:
                gap = np.zeros(int(sr * cl.pause), np.float32)
                nxt = clauses[i + 1] if i + 1 < len(clauses) else None
                long_next = nxt is not None and nxt.sentence_start and (
                    len(re.findall(r"[\u3400-\u9fff]", nxt.text)) >= 12 or len(nxt.text.split()) >= 8)
                if cfg.get("breaths") and long_next and cl.pause >= cfg["sentence"] * 0.8 and rng.random() < float(cfg["breath_prob"]):
                    b = _breath(job, sr, speech_db, rng)
                    if b is not None:
                        lead = max(int(sr * 0.06), len(gap) - len(b) - int(sr * 0.08))
                        gap = np.zeros(lead + len(b) + int(sr * 0.08), np.float32)
                        gap[lead: lead + len(b)] = b
                await out(gap, silent=True)
        if post is not None and sr:
            t = await asyncio.to_thread(post.tail_out)
            if len(t):
                await _emit(job, sr, _to_pcm(t))
    except EngineUnavailable as e:
        job.error = str(e)
    except Exception as e:  # surface anything else as a clean message
        job.error = f"合成失败：{e}"
    finally:
        if job.utterance is not None:
            job.utterance.finish(job.error)
        if job.live is not None:
            await job.live.put(None)
        job.done.set()


def start(text: str, voice: str | None = None, language: str | None = None, play: bool | None = None,
          speed: float | None = None, live: bool = False, streaming_mode: int | None = None,
          emotion: str | None = None, tweak: dict | None = None, preset: str | None = None,
          naturalness: bool | str | dict | None = None) -> SpeakJob:
    """Begin synthesis (and playback). Raises LookupError/ValueError for bad input.

    tweak / preset adjust the voice on top of its saved defaults (see voicefx)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("要说的文字不能为空。")
    if len(text) > 4000:
        raise ValueError("文字太长（上限 4000 字）。请分段调用。")
    v = voices.find(voice)
    req = build_request(v, text, language, speed, streaming_mode, emotion)
    tw = voicefx.resolve(v.get("tweak"), preset, tweak)
    nat = natural.resolve(config.load_settings().get("natural"), naturalness)
    if play is None:
        play = bool(config.load_settings()["speech"]["play_locally"])
    job = SpeakJob(voice=v, text=text, request=req, play=play, live=asyncio.Queue() if live else None, tweak=tw, natural=nat)
    if play:
        job.utterance = player.enqueue(Utterance(voice=v["id"], text=text))
    asyncio.create_task(_produce(job))
    return job


async def stream(job: SpeakJob) -> AsyncIterator[tuple[int, np.ndarray]]:
    assert job.live is not None
    while True:
        item = await job.live.get()
        if item is None:
            return
        yield item
