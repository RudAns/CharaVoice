"""Text-to-speech orchestration: voice lookup, language choice, local playback, tee to clients."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import AsyncIterator

import numpy as np

from . import config, voices
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
    ref = voices.pick_ref(voice, emotion)
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

    def audio(self) -> np.ndarray:
        return np.concatenate(self.chunks) if self.chunks else np.zeros(0, dtype=np.int16)


async def _produce(job: SpeakJob) -> None:
    try:
        async for sr, pcm in engine_for(job.request.engine).synthesize(job.request):
            if job.utterance is not None and job.utterance.cancelled and job.live is None:
                break
            job.sr = sr
            job.chunks.append(pcm)
            if job.utterance is not None:
                job.utterance.feed(sr, pcm)
            if job.live is not None:
                await job.live.put((sr, pcm))
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
          emotion: str | None = None) -> SpeakJob:
    """Begin synthesis (and playback). Raises LookupError/ValueError for bad input."""
    text = (text or "").strip()
    if not text:
        raise ValueError("要说的文字不能为空。")
    if len(text) > 4000:
        raise ValueError("文字太长（上限 4000 字）。请分段调用。")
    v = voices.find(voice)
    req = build_request(v, text, language, speed, streaming_mode, emotion)
    if play is None:
        play = bool(config.load_settings()["speech"]["play_locally"])
    job = SpeakJob(voice=v, text=text, request=req, play=play, live=asyncio.Queue() if live else None)
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
