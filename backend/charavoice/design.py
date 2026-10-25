"""Design a voice from a text description (OmniVoice voice-design mode, zh / en).

The same description gives a different speaker on every run, so the flow is: generate a few
candidates, let the user pick one, and save that clip as the new voice's reference audio. Cloning
from it keeps the designed voice stable (measured speaker similarity 0.74-0.84 to the candidate).
With a reference audio present OmniVoice ignores or misreads these attributes, so they are only
offered here, not as per-request options.
"""

from __future__ import annotations

import secrets
import time

import httpx
import numpy as np
import soundfile as sf

from . import config
from .engine import EngineUnavailable, engine_for

GENDERS = {"male": "男", "female": "女"}
AGES = {"child": "儿童", "teenager": "少年", "young adult": "青年", "middle-aged": "中年", "elderly": "老年"}
PITCHES = {"very low pitch": "极低音调", "low pitch": "低音调", "moderate pitch": "中音调", "high pitch": "高音调", "very high pitch": "极高音调"}
ACCENTS = ["american accent", "british accent", "australian accent", "canadian accent", "indian accent"]
# long enough for a 5-8 s reference clip (GPT-SoVITS bases need 3-10 s if the user switches later)
SAMPLE = {
    "zh": "你好，很高兴认识你。今天天气不错，有什么我可以帮你的吗？我们慢慢聊。",
    "en": "Hello, it is really nice to meet you. The weather is lovely today. Is there anything I can help you with?",
}
DIR = config.CACHE_DIR / "design"
KEEP_SECONDS = 24 * 3600


def instruct(gender: str | None, age: str | None, pitch: str | None, whisper: bool, accent: str | None, lang: str) -> str:
    """Build OmniVoice's instruct string; Chinese tags for Chinese text, English tags otherwise."""
    parts = []
    for val, table in ((gender, GENDERS), (age, AGES), (pitch, PITCHES)):
        if val:
            if val not in table:
                raise ValueError(f"不支持的属性：{val}")
            parts.append(table[val] if lang == "zh" else val)
    if whisper:
        parts.append("耳语" if lang == "zh" else "whisper")
    if accent and lang == "en":
        if accent not in ACCENTS:
            raise ValueError(f"不支持的口音：{accent}")
        parts.append(accent)
    if not parts:
        raise ValueError("至少选一个属性（性别、年龄或音高）。")
    return ("，" if lang == "zh" else ", ").join(parts)


def _cleanup() -> None:
    if not DIR.exists():
        return
    now = time.time()
    for p in DIR.glob("*.wav"):
        if now - p.stat().st_mtime > KEEP_SECONDS:
            p.unlink(missing_ok=True)


async def candidates(ins: str, lang: str, text: str | None, n: int = 3) -> list[dict]:
    if lang not in SAMPLE:
        raise ValueError("文字设计声音只支持中文和英文。")
    eng = engine_for("omnivoice")
    if not eng.installed():
        raise EngineUnavailable(eng.missing_message())
    await eng.ensure_ready(wait=600)
    text = (text or "").strip() or SAMPLE[lang]
    DIR.mkdir(parents=True, exist_ok=True)
    _cleanup()
    out = []
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{eng.port}", timeout=300) as c:
        for _ in range(max(1, min(5, n))):
            r = await c.post("/tts", json={"text": text, "language": lang, "instruct": ins, "stream": False, "steps": 16})
            if r.status_code != 200:
                raise EngineUnavailable(f"OmniVoice 生成失败：{r.text[:200]}", "synthesis_failed")
            sr = int.from_bytes(r.content[24:28], "little")
            x = np.frombuffer(r.content[44:], "<i2")
            if len(x) < sr:
                raise EngineUnavailable("OmniVoice 没有生成有效音频，请换一组描述再试。", "synthesis_failed")
            cid = secrets.token_hex(6)
            sf.write(DIR / f"{cid}.wav", x, sr, subtype="PCM_16")
            out.append({"id": cid, "url": f"/v1/voices/design/{cid}/audio", "duration": round(len(x) / sr, 2), "text": text, "language": lang})
    return out


def candidate_path(cid: str):
    if not cid.isalnum():
        raise LookupError("无效的候选编号")
    p = DIR / f"{cid}.wav"
    if not p.exists():
        raise LookupError("候选音频已过期，请重新生成。")
    return p
