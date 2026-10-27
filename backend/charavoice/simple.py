"""Simple mode: material folders for cloning one person's voice (Japanese, Chinese or English).

A material folder is a voice (language ja, flagged "folder": true). Saved clips live in
data/voices/<id>/clips/<cid>.wav with metadata in clips.json; the recordings they were cut
from stay in data/recordings/ (FLAC) so a clip can be trimmed again later.

Cloning a folder runs every clip through the material pipeline (dataset.py: vocal separation,
sentence split, transcript, speaker check), sets the cleanest sentence as the zero-shot
reference and, with a minute or more of usable speech, fine-tunes v2ProPlus (training.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from . import asr, audio_util, config, dataset, training, voices
from .events import bus
from .recorder import recorder

log = logging.getLogger("charavoice.simple")
LANG = "ja"  # default for folders created before languages were per folder
LANGS = ("ja", "zh", "en")
WARM_TEXT = {"ja": "こんにちは。", "zh": "你好。", "en": "Hello."}


def lang_of(vid: str) -> str:
    """The language of a folder's material (what its sentences are transcribed and trained in)."""
    try:
        lang = voices.get(vid).get("language")
    except KeyError:
        return LANG
    return lang if lang in LANGS else LANG


# characters that only exist in simplified Chinese (Japanese uses the traditional or its own form)
_SIMPLIFIED = re.compile(r"[们这说时个们为对还进这么样么吗呢吧见话语让谁给们边过还经点门问间东车长开关电见现]")
_ZH_PUNCT = re.compile(r"[，；：“”？！]")


def text_lang(text: str, folder_lang: str) -> str:
    """Language to synthesize `text` in (when the caller did not say).
    - kana -> Japanese (normal Japanese sentences nearly always contain kana)
    - CJK without kana -> Chinese, except a short word of 1-4 characters with no Chinese-only
      character forms or Chinese punctuation, which follows the folder (a Japanese name or noun)
    - Latin only -> English
    Chinese and Japanese keep embedded English words (GPT-SoVITS zh / ja read zh+en and ja+en)."""
    if re.search(r"[぀-ヿ]", text):
        return "ja"
    han = re.findall(r"[㐀-鿿]", text)
    if han:
        short = len(han) <= 4 and not _SIMPLIFIED.search(text) and not _ZH_PUNCT.search(text)
        return "ja" if folder_lang == "ja" and short else "zh"
    if re.search(r"[A-Za-z]", text):
        return "en"
    return folder_lang


ACCENT_NOTE = {"ja": "日语", "zh": "中文", "en": "英语"}


def accent_warning(folder_lang: str, spoken: str) -> str | None:
    if spoken == folder_lang:
        return None
    return (f"这个声音是用{ACCENT_NOTE.get(folder_lang, folder_lang)}素材训练的，说{ACCENT_NOTE.get(spoken, spoken)}可能带外语口音；"
            f"想要自然的{ACCENT_NOTE.get(spoken, spoken)}，请用这个人说{ACCENT_NOTE.get(spoken, spoken)}的素材。")
REC_VOICE = "__simple__"  # Recording.voice for hotkey recordings (not a real voice)
SAME_SPEAKER = 0.5  # CAM++ cosine: same person usually 0.6-0.85, different people below 0.4
CLIP_GAP = 0.3  # seconds of silence between kept ranges inside one saved clip
TRAIN_BASE = "v2ProPlus"
# De-esser levels for a folder's voice. "Adaptive" thresholds start at the voice's own normal
# sibilance (median 4.5-11 kHz share of its sibilant 2 ms blocks), so only s/sh louder than this
# speaker usually makes them is reduced; "strong" is the fixed -9 dB threshold, which also touches
# ordinary fricatives and consonant edges (measured: 26-34 % of voiced blocks vs about 4 %).
DEESS_LEVELS = ("off", "light", "medium", "strong")
DEESS_DEFAULT = "medium"
SIB_NORM_FALLBACK = -4.0

# How a folder's voice is synthesized. "trained" uses the folder's own GPT-SoVITS result (zero-shot
# or fine-tuned); "omnivoice" clones it zero-shot with OmniVoice from the folder's cleanest sentences.
# Measured on a folder fine-tuned from 100 s of music-separated anime audio: the fine-tuned vocoder
# renders s/sh as featureless noise (4.5-11 kHz spectral flatness 0.47 vs 0.27 for studio speech),
# OmniVoice keeps their structure (0.28) at a slightly lower speaker similarity (0.81 vs 0.86).
MODES = ("trained", "omnivoice")
OMNI_REF_MAX = 10.0  # seconds: the cleanest sentences are joined up to this length

# Reading speed for articles: cloned anime/game voices copy their source's fast delivery, and
# 0.9 brings a typical 6.5-7 chars/s down to a relaxed reading pace.
READ_SPEED = 0.9
_lock = threading.RLock()


def rec_dir() -> Path:
    return config.DATA_DIR / "recordings"


def export_dir() -> Path:
    return config.DATA_DIR / "exports"


# ------------------------------------------------------------------ consent (asked once)
def consent() -> str | None:
    return config.load_settings().get("simple", {}).get("consent_at")


def set_consent() -> str:
    at = voices.now_iso()
    config.save_settings({"simple": {"consent_at": at}})
    return at


# ------------------------------------------------------------------ folders
def _clips_path(vid: str) -> Path:
    return voices.folder(vid) / "clips.json"


def clip_dir(vid: str) -> Path:
    return voices.folder(vid) / "clips"


def load_clips(vid: str) -> dict[str, Any]:
    p = _clips_path(vid)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"clips": [], "cloned": None}


def _save_clips(vid: str, data: dict[str, Any]) -> None:
    p = _clips_path(vid)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    bus.publish("simple_changed", folder=vid)


def _fingerprint(clips: list[dict[str, Any]]) -> str:
    s = "|".join(f"{c['id']}:{c['duration']}" for c in sorted(clips, key=lambda c: c["id"]))
    return hashlib.sha1(s.encode()).hexdigest()[:12]


def est_minutes(seconds: float, train: bool) -> int:
    """Rough wall-clock estimate on a 12 GB card: separation + transcripts, then fine-tuning
    (10 minutes of speech trained in about 6 minutes on an RTX 3060)."""
    m = 0.5 + 0.25 * seconds / 60
    if train:
        m += 3 + 0.3 * seconds / 60
    return max(1, math.ceil(m))


def is_folder(v: dict[str, Any]) -> bool:
    return bool(v.get("folder"))


def summary(v: dict[str, Any]) -> dict[str, Any]:
    vid = v["id"]
    data = load_clips(vid)
    secs = round(sum(c["duration"] for c in data["clips"]), 1)
    cloned = data.get("cloned")
    j = jobs.get(vid)
    tags = sorted({c["tag"] for c in data["clips"] if c.get("tag")})
    return {
        "id": vid,
        "name": v["name"],
        "clips": len(data["clips"]),
        "seconds": secs,
        "tags": tags,
        "ready": bool(v.get("ref")),
        "trained": bool((v.get("weights") or {}).get("sovits")),
        "cloned": cloned,
        "dirty": bool(cloned) and (cloned.get("fingerprint") != _fingerprint(data["clips"])
                                   or data.get("texts_edited", "") > cloned.get("at", "")
                                   or data.get("lang_changed", "") > cloned.get("at", "")),
        "texts_edited": bool(cloned) and data.get("texts_edited", "") > cloned.get("at", ""),
        "job": j.public() if j else None,
        "path": str(clip_dir(vid).resolve()),
        "est": {"zero_shot": est_minutes(secs, False), "train": est_minutes(secs, True)},
        "read_speed": data.get("read_speed", READ_SPEED),
        "deess": data.get("deess", DEESS_DEFAULT),
        "mode": data.get("mode", "trained"),
        "language": v.get("language") if v.get("language") in LANGS else LANG,
        "lang_auto": bool(data.get("lang_auto")),
        "models": [{k: m[k] for k in ("id", "label", "note")} for m in data.get("models", [])],
        "model": data.get("model"),
        "doubtful": _doubtful_count(vid),
    }


def _doubtful_count(vid: str) -> int:
    try:
        items = dataset.load(vid)["items"]
    except KeyError:
        return 0
    lang = lang_of(vid)
    def alt(i):
        return i.get("asr_alt") if i.get("asr_alt_lang") == lang else (i.get("asr_ja") if lang == "ja" else None)
    return sum(1 for i in items if not i.get("deleted") and i.get("text", "") == i.get("asr_text", "")
               and alt(i) is not None and _doubt(i.get("text", ""), i["duration"], alt(i) or None, lang))


def folders() -> list[dict[str, Any]]:
    return [summary(v) for v in voices.list_all() if is_folder(v)]


def create_folder(name: str, language: str | None = None) -> dict[str, Any]:
    """language: ja / zh / en, or None to detect it from the first clip saved into the folder."""
    if not consent():
        raise ValueError("请先确认只录制、克隆本人或已获授权的声音。")
    v = voices.create(name, "authorized", language if language in LANGS else LANG)
    v = voices.update(v["id"], {"folder": True})
    clip_dir(v["id"]).mkdir(parents=True, exist_ok=True)
    _save_clips(v["id"], {"clips": [], "cloned": None, "lang_auto": language not in LANGS})
    return summary(voices.get(v["id"]))


def set_language(vid: str, language: str) -> str:
    if language not in LANGS:
        raise ValueError("语言只能是 ja / zh / en")
    voices.update(vid, {"language": language})
    with _lock:
        data = load_clips(vid)
        data["lang_auto"] = False
        data["lang_changed"] = voices.now_iso()  # the sentences must be transcribed again
        _save_clips(vid, data)
    return language


def _detect_language(vid: str, wav: Path) -> None:
    """First clip of a folder created without a language: let SenseVoice tell (ja / zh / en)."""
    data = load_clips(vid)
    if not data.get("lang_auto"):
        return
    try:
        x = audio_util.decode(wav, 16000)[: 16000 * 30]
        segs = asr.segment(x, min_silence=0.3, min_speech=0.5, max_speech=12.0)[:6]
        votes: dict[str, float] = {}
        for _, seg in segs:
            r = asr.transcribe(seg)
            if r["language"] in LANGS:
                votes[r["language"]] = votes.get(r["language"], 0) + len(seg)
    except Exception as e:
        log.info("language detection failed: %s", e)
        return
    if not votes:
        return
    lang = max(votes, key=votes.get)
    voices.update(vid, {"language": lang})
    with _lock:
        data = load_clips(vid)
        data["lang_auto"] = False
        _save_clips(vid, data)
    log.info("folder %s: detected language %s", vid, lang)


def set_read_speed(vid: str, speed: float) -> float:
    with _lock:
        data = load_clips(vid)
        data["read_speed"] = round(float(min(1.3, max(0.6, speed))), 2)
        _save_clips(vid, data)
    return data["read_speed"]


def set_deess(vid: str, level: str) -> str:
    if level not in DEESS_LEVELS:
        raise ValueError(f"去齿音强度只能是 {' / '.join(DEESS_LEVELS)}")
    with _lock:
        data = load_clips(vid)
        data["deess"] = level
        _save_clips(vid, data)
    return level


def set_mode(vid: str, mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"合成方式只能是 {' / '.join(MODES)}")
    if mode == "omnivoice":
        from .engine import omni

        if not omni.installed():
            raise ValueError("没有安装 OmniVoice 引擎，请在“高级 → 模型”页安装。")
        if not load_clips(vid).get("omni_ref"):
            _pick_omni_ref(vid)
    with _lock:
        data = load_clips(vid)
        data["mode"] = mode
        _save_clips(vid, data)
    return mode


def _pick_omni_ref(vid: str) -> dict[str, Any]:
    """OmniVoice takes one reference clip: join the folder's cleanest sentences (least accompaniment
    before separation, transcript not in doubt, 3-8 s each) into one clip of up to 10 s."""
    items = [i for i in dataset.load(vid)["items"] if not i.get("deleted") and i.get("text") and 2.5 <= i["duration"] <= 8.0]
    if not items:
        raise ValueError("文件夹还没有整理好的句子，请先点“克隆这个声线”。")
    sure = [i for i in items if not _doubt(i["text"], i["duration"], i.get("asr_alt") or i.get("asr_ja") or None, lang_of(vid))] or items
    sure.sort(key=lambda i: (i["quality"].get("bgm_db", 0), -i["score"]))
    picked, total = [], 0.0
    for i in sure:
        if total + i["duration"] > OMNI_REF_MAX and picked:
            continue
        picked.append(i)
        total += i["duration"] + 0.3
        if total >= OMNI_REF_MAX - 2.5:
            break
    sr = 32000
    gap = np.zeros(int(0.3 * sr), np.float32)
    parts = []
    for i in picked:
        x = audio_util.decode(dataset.ddir(vid) / "clips" / f"{i['id']}.wav", sr)
        parts += [x, gap]
    y = np.concatenate(parts[:-1])
    out = voices.folder(vid) / "ref" / "omni.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), y, sr, subtype="PCM_16")
    joiner = " " if lang_of(vid) == "en" else ""
    ref = {"audio": "ref/omni.wav", "text": joiner.join(i["text"] for i in picked), "lang": lang_of(vid),
           "duration": round(len(y) / sr, 2), "items": [i["id"] for i in picked]}
    with _lock:
        data = load_clips(vid)
        data["omni_ref"] = ref
        _save_clips(vid, data)
    return ref


def route(v: dict[str, Any]) -> dict[str, Any]:
    """The voice as synthesis should see it: a folder in OmniVoice mode becomes an OmniVoice
    zero-shot voice with the folder's cleanest sentences as reference."""
    try:
        data = load_clips(v["id"])
    except KeyError:
        return v
    if data.get("mode") != "omnivoice" or not data.get("omni_ref"):
        return v
    return {**v, "base": "omnivoice", "ref": data["omni_ref"], "refs": [], "aux_refs": []}


def _sib_norm(vid: str) -> float:
    """The speaker's normal sibilance level (dB), measured once from the folder's sentences."""
    data = load_clips(vid)
    if "sib_norm" in data:
        return float(data["sib_norm"])
    from . import natural

    rs = []
    for f in sorted((dataset.ddir(vid) / "clips").glob("*.wav"))[:60]:
        x, sr = sf.read(str(f), dtype="float32")
        d = natural.DeEsser(sr, 0.0)
        buf = np.concatenate([np.zeros(len(d.h) - 1, np.float32), x])
        band = np.convolve(buf, d.h, mode="valid")
        dry = buf[len(d.h) - 1 - d.d : len(buf) - d.d]
        n = len(x) // d.blk
        eb = np.sqrt(np.mean(band[: n * d.blk].reshape(n, -1) ** 2, 1) + 1e-12)
        ex = np.sqrt(np.mean(dry[: n * d.blk].reshape(n, -1) ** 2, 1) + 1e-12)
        r = 20 * np.log10(eb / ex)[ex > 1e-3]
        rs.append(r[r > -9])
    sib = np.concatenate(rs) if rs else np.zeros(0)
    if len(sib) < 50:
        return SIB_NORM_FALLBACK  # not cloned yet: no sentences to measure
    norm = round(float(np.median(sib)), 1)
    with _lock:
        data = load_clips(vid)
        data["sib_norm"] = norm
        _save_clips(vid, data)
    return norm


def deess_cfg(vid: str) -> dict[str, Any]:
    """Naturalness overrides for synthesis with this folder's voice."""
    try:
        level = load_clips(vid).get("deess", DEESS_DEFAULT)
    except KeyError:
        return {}
    if level == "off":
        return {"deess": False}
    if level == "strong":
        return {"deess": True, "deess_db": 8.0, "deess_thr": -9.0}
    norm = _sib_norm(vid)
    return {"deess": True, "deess_db": 4.0, "deess_thr": norm + 2.0} if level == "light" else \
        {"deess": True, "deess_db": 6.0, "deess_thr": norm}


def read_speed(vid: str) -> float:
    return float(load_clips(vid).get("read_speed", READ_SPEED))


def folder_detail(vid: str) -> dict[str, Any]:
    v = voices.get(vid)
    data = load_clips(vid)
    return {**summary(v), "items": [{k: c[k] for k in c if k != "embedding"} for c in data["clips"]]}


# ------------------------------------------------------------------ hotkey recording
def _rec_path(rid: str) -> Path:
    if not re.fullmatch(r"[a-z0-9-]{1,60}", rid or ""):
        raise KeyError(rid)
    p = rec_dir() / f"{rid}.flac"
    if not p.exists():
        raise KeyError(rid)
    return p


def recording_now() -> dict[str, Any]:
    rec = recorder.current
    if not rec or rec.stopped.is_set():
        return {"recording": False}
    return {"recording": True, "simple": rec.voice == REC_VOICE, "seconds": round(rec.seconds, 2), "device": rec.device.get("name")}


def record_start(device: int | None = None) -> dict[str, Any]:
    if not consent():
        raise PermissionError("consent_required")
    if device is None:
        device = config.load_settings().get("simple", {}).get("device")
    rec = recorder.start(REC_VOICE, rec_dir(), device)
    return {"recording": True, "id": rec.id, "device": rec.device.get("name")}


def record_stop() -> dict[str, Any]:
    rec = recorder.current
    if not rec or rec.stopped.is_set():
        raise ValueError("当前没有在录音。")
    if rec.voice != REC_VOICE:
        raise ValueError("“素材”页正在录音，请在那里停止。")
    recorder.stop()
    rec.stopped.wait(10)
    if rec.seconds < 1.0:
        rec.path.unlink(missing_ok=True)
        raise ValueError(rec.error or "录音太短（不到 1 秒），已丢弃。")
    # FLAC halves the size; the recording is kept so saved clips can be trimmed again
    x, sr = sf.read(str(rec.path), dtype="int16", always_2d=True)
    flac = rec.path.with_suffix(".flac")
    sf.write(str(flac), x, sr, format="FLAC", subtype="PCM_16")
    rec.path.unlink(missing_ok=True)
    out = {"rec": flac.stem, "seconds": round(len(x) / sr, 2), "error": rec.error}
    bus.publish("simple_recorded", **out)
    return out


def record_toggle(device: int | None = None) -> dict[str, Any]:
    rec = recorder.current
    if rec and not rec.stopped.is_set():
        return {"recording": False, **record_stop()}
    return record_start(device)


def rec_file(rid: str) -> Path:
    return _rec_path(rid)


def _peaks(path: Path, per_second: int) -> dict[str, Any]:
    x, sr = sf.read(str(path), dtype="float32", always_2d=True)
    m = x.mean(axis=1)
    per_second = max(10, min(400, per_second))
    hop = max(1, sr // per_second)
    n = len(m) // hop
    blk = m[: n * hop].reshape(n, hop)
    return {
        "rate": sr / hop,
        "duration": round(len(m) / sr, 3),
        "min": np.round(blk.min(axis=1), 3).tolist(),
        "max": np.round(blk.max(axis=1), 3).tolist(),
    }


def rec_peaks(rid: str, per_second: int = 100) -> dict[str, Any]:
    return _peaks(_rec_path(rid), per_second)


def _embed(x16: np.ndarray) -> np.ndarray | None:
    try:
        return dataset._embed(x16)
    except Exception as e:  # speaker model missing: trimming still works, just without the check
        log.info("speaker embedding unavailable: %s", e)
        return None


def _folder_centroid(vid: str | None) -> np.ndarray | None:
    if not vid:
        return None
    embs = [np.array(c["embedding"], np.float32) for c in load_clips(vid)["clips"] if c.get("embedding")]
    if not embs:
        return None
    t = np.mean(embs, axis=0)
    return t / (np.linalg.norm(t) + 1e-9)


def _speech_bounds(segs: list[tuple[float, float]], duration: float) -> list[float]:
    if not segs:
        return [0.0, round(duration, 2)]
    return [round(max(0.0, segs[0][0] - 0.2), 2), round(min(duration, segs[-1][1] + 0.3), 2)]


def analyze(rid: str, folder: str | None = None) -> dict[str, Any]:
    """Speech segments (Silero VAD) with suggested start/end, and speaker similarity between
    segments and to the folder's saved clips, so segments of other people can be marked."""
    x16 = audio_util.decode(_rec_path(rid), 16000)
    duration = len(x16) / 16000
    raw = asr.segment(x16, min_silence=0.3, min_speech=0.3, max_speech=12.0)
    segs = [(s / 16000, (s + len(seg)) / 16000) for s, seg in raw]
    embs: list[np.ndarray | None] = []
    for (a, b), (_, seg) in zip(segs, raw):
        embs.append(_embed(seg) if b - a >= 0.8 else None)
    centroid = _folder_centroid(folder)
    ok = [e for e in embs if e is not None]
    n = len(segs)
    sims = [[None] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if embs[i] is not None and embs[j] is not None:
                sims[i][j] = round(float(np.dot(embs[i], embs[j])), 3)
    return {
        "duration": round(duration, 3),
        "suggest": _speech_bounds(segs, duration),
        "segments": [
            {"a": round(a, 2), "b": round(b, 2),
             "folder_sim": round(float(np.dot(e, centroid)), 3) if e is not None and centroid is not None else None}
            for (a, b), e in zip(segs, embs)
        ],
        "sims": sims,
        "speaker_check": bool(ok),
        "has_folder_ref": centroid is not None,
        "threshold": SAME_SPEAKER,
    }


def discard_recording(rid: str) -> bool:
    """Delete a recording no saved clip was cut from."""
    p = _rec_path(rid)
    for v in voices.list_all():
        if is_folder(v) and any(c.get("rec") == rid for c in load_clips(v["id"])["clips"]):
            return False
    p.unlink(missing_ok=True)
    return True


def _gc(rids: set[str]) -> None:
    """Drop recordings that no clip refers to any more (they cannot be reached from the UI)."""
    for rid in rids:
        try:
            discard_recording(rid)
        except KeyError:
            pass


def gc_orphans(max_age_days: float = 3.0) -> int:
    """Delete recordings no clip refers to once they are a few days old (left open in the trim
    editor when the app closed, or replaced by a newer recording before saving)."""
    used = {c.get("rec") for v in voices.list_all() if is_folder(v) for c in load_clips(v["id"])["clips"]}
    n = 0
    for p in rec_dir().glob("*.flac") if rec_dir().exists() else []:
        if p.stem not in used and time.time() - p.stat().st_mtime > max_age_days * 86400:
            p.unlink(missing_ok=True)
            n += 1
    for p in rec_dir().glob("*.wav") if rec_dir().exists() else []:  # interrupted recordings
        if time.time() - p.stat().st_mtime > max_age_days * 86400:
            p.unlink(missing_ok=True)
    return n


def delete_folder(vid: str) -> None:
    rids = {c["rec"] for c in load_clips(vid)["clips"] if c.get("rec")}
    voices.delete(vid)
    _gc(rids)


# ------------------------------------------------------------------ clips
def _clip(data: dict[str, Any], cid: str) -> dict[str, Any]:
    for c in data["clips"]:
        if c["id"] == cid:
            return c
    raise KeyError(cid)


def clip_file(vid: str, cid: str) -> Path:
    return clip_dir(vid) / _clip(load_clips(vid), cid)["file"]


def save_clip(vid: str, rid: str, ranges: list[list[float]], name: str | None = None, tag: str | None = None,
              replace: str | None = None) -> dict[str, Any]:
    """Cut the kept ranges out of a recording into one clip (ranges joined by short silence)."""
    voices.get(vid)
    x, sr = sf.read(str(_rec_path(rid)), dtype="float32", always_2d=True)
    mono = x.mean(axis=1)
    kept = dataset.normalize_ranges(ranges, len(mono) / sr)
    if not kept:
        raise ValueError("没有选中任何声音。")
    gap = np.zeros(int(CLIP_GAP * sr), np.float32)
    pieces: list[np.ndarray] = []
    for a, b in kept:
        pieces += [mono[int(a * sr) : int(b * sr)], gap]
    y = np.concatenate(pieces[:-1])
    if len(y) < sr * 0.5:
        raise ValueError("截取的片段太短（不到 0.5 秒）。")
    cid = uuid.uuid4().hex[:8]
    d = clip_dir(vid)
    d.mkdir(parents=True, exist_ok=True)
    sf.write(str(d / f"{cid}.wav"), np.clip(y, -1, 1), sr, subtype="PCM_16")
    e = _embed(audio_util.decode(d / f"{cid}.wav", 16000))
    with _lock:
        data = load_clips(vid)
        clip = {
            "id": cid, "name": (name or "").strip(), "file": f"{cid}.wav", "duration": round(len(y) / sr, 2),
            "created": voices.now_iso(), "tag": (tag or "").strip(), "rec": rid, "ranges": [list(k) for k in kept],
            "embedding": [round(float(v), 5) for v in e] if e is not None else None,
        }
        if replace:
            old = _clip(data, replace)
            clip.update(name=old["name"] if name is None else clip["name"], tag=old.get("tag", "") if tag is None else clip["tag"])
            (d / old["file"]).unlink(missing_ok=True)
            data["clips"] = [clip if c["id"] == replace else c for c in data["clips"]]
        else:
            if not clip["name"]:
                clip["name"] = f"片段 {len(data['clips']) + 1}"
            data["clips"].append(clip)
        _save_clips(vid, data)
    if data.get("lang_auto"):
        _detect_language(vid, d / f"{cid}.wav")
    return {k: clip[k] for k in clip if k != "embedding"}


def import_file(vid: str, name: str, raw: bytes | None = None, path: Path | None = None) -> dict[str, Any]:
    """Import an audio file as a clip: keep the original as a recording (so it can be trimmed
    later) and save the part between the first and last speech."""
    voices.get(vid)
    rec_dir().mkdir(parents=True, exist_ok=True)
    tmp = rec_dir() / f"tmp-{uuid.uuid4().hex[:6]}{Path(name).suffix.lower() or '.bin'}"
    try:
        if raw is not None:
            tmp.write_bytes(raw)
            src = tmp
        else:
            assert path is not None
            src = path
        stereo = dataset._decode_stereo(src)
    finally:
        tmp.unlink(missing_ok=True)
    if len(stereo) < 44100:
        raise ValueError(f"“{name}”太短或没有音轨（至少 1 秒）。")
    rid = f"imp-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    sf.write(str(rec_dir() / f"{rid}.flac"), stereo, 44100, format="FLAC", subtype="PCM_16")
    x16 = audio_util.decode(rec_dir() / f"{rid}.flac", 16000)
    segs = [(s / 16000, (s + len(seg)) / 16000) for s, seg in asr.segment(x16, min_silence=0.3, min_speech=0.3, max_speech=12.0)]
    return save_clip(vid, rid, [_speech_bounds(segs, len(x16) / 16000)], name=Path(name).stem)


def patch_clip(vid: str, cid: str, name: str | None = None, tag: str | None = None) -> dict[str, Any]:
    with _lock:
        data = load_clips(vid)
        c = _clip(data, cid)
        if name is not None and name.strip():
            c["name"] = name.strip()[:60]
        if tag is not None:
            c["tag"] = tag.strip()[:20]
        _save_clips(vid, data)
    return {k: c[k] for k in c if k != "embedding"}


def delete_clips(vid: str, ids: list[str]) -> int:
    with _lock:
        data = load_clips(vid)
        gone = [c for c in data["clips"] if c["id"] in set(ids)]
        data["clips"] = [c for c in data["clips"] if c["id"] not in set(ids)]
        for c in gone:
            (clip_dir(vid) / c["file"]).unlink(missing_ok=True)
        _save_clips(vid, data)
    _gc({c["rec"] for c in gone if c.get("rec")})
    return len(gone)


def move_clips(src: str, dst: str, ids: list[str]) -> int:
    if src == dst:
        return 0
    voices.get(dst)
    with _lock:
        a, b = load_clips(src), load_clips(dst)
        moving = [c for c in a["clips"] if c["id"] in set(ids)]
        clip_dir(dst).mkdir(parents=True, exist_ok=True)
        for c in moving:
            (clip_dir(src) / c["file"]).replace(clip_dir(dst) / c["file"])
        a["clips"] = [c for c in a["clips"] if c["id"] not in set(ids)]
        b["clips"] += moving
        _save_clips(src, a)
        _save_clips(dst, b)
    return len(moving)


# ------------------------------------------------------------------ clone (whole folder)
@dataclass
class CloneJob:
    voice: str
    train: bool
    separate: bool
    state: str = "running"  # running | done | error | cancelled
    phase: str = "material"  # material | train | done
    stage: str = "准备素材"
    progress: float = 0.0
    message: str = ""
    mode: str = ""  # zero-shot | trained
    seconds: float = 0.0
    started: float = field(default_factory=time.time)
    cancel: bool = False

    def update(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)
        bus.publish("simple_job", **self.public())

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("cancel")
        d["elapsed"] = round(time.time() - self.started)
        return d


jobs: dict[str, CloneJob] = {}


def start_clone(vid: str, train: bool = True, separate: bool = True) -> CloneJob:
    voices.get(vid)
    with _lock:
        j = jobs.get(vid)
        if j and j.state == "running":
            raise ValueError("这个文件夹正在学习。")
        if not load_clips(vid)["clips"]:
            raise ValueError("文件夹里还没有片段。先录制并保存几段声音。")
        dataset._busy(vid)
        job = CloneJob(voice=vid, train=train, separate=separate)
        jobs[vid] = job
    threading.Thread(target=_clone, args=(job,), daemon=True, name=f"clone-{vid}").start()
    return job


def cancel_clone(vid: str) -> bool:
    j = jobs.get(vid)
    if not j or j.state != "running":
        return False
    j.cancel = True
    dataset.cancel(vid)
    training.cancel(vid)
    return True


class _Cancelled(Exception):
    pass


def _clone(job: CloneJob) -> None:
    vid = job.voice
    try:
        data = load_clips(vid)
        fingerprint = _fingerprint(data["clips"])
        prev = data.get("cloned") or {}
        reuse = (prev.get("fingerprint") == fingerprint and bool(dataset.load(vid)["items"])
                 and data.get("lang_changed", "") <= prev.get("at", ""))
        if reuse:
            # same clips as last time: keep the processed sentences, the user's text corrections,
            # their include/exclude choices and the reference; only (re)train
            job.update(stage="沿用已整理的句子和修改过的转写", progress=0.4)
        else:
            _material(job, vid, data)
            # 2. the folder's language; keep the folder's main speaker
            job.update(stage="筛选说话人", progress=0.41)
            _keep_main_speaker(vid)
            job.update(stage="复核转写", progress=0.42)
            _second_opinion(vid)
            with _lock:
                d = load_clips(vid)
                d.pop("sib_norm", None)
                _save_clips(vid, d)
        # 3. zero-shot reference: the cleanest 3-10 s sentence (kept if the user already set one)
        v = voices.get(vid)
        ref = v.get("ref") if reuse and v.get("ref") else _pick_ref(vid)
        if not ref:
            raise RuntimeError("没有识别出可用的语句。请确认片段里是清晰的人声，文件夹的语言也设对了。")
        secs = dataset.summary(vid)["seconds"]
        job.seconds = secs
        _after_material(job, vid, fingerprint, secs)
    except _Cancelled:
        job.update(state="cancelled", stage="已取消", message="已取消。" + ("零样本克隆已可用。" if job.phase == "train" else ""))
    except Exception as e:
        log.exception("clone failed")
        job.update(state="error", stage="出错", message=str(e))


def _material(job: CloneJob, vid: str, data: dict[str, Any]) -> None:
    """Material pipeline over every clip of the folder (from scratch)."""
    job.update(stage="准备素材", progress=0.01)
    dataset.clear(vid)
    todo = []
    for c in data["clips"]:
        src = dataset.add_source(vid, c["name"] or c["id"], raw=(clip_dir(vid) / c["file"]).read_bytes())
        todo.append((src["id"], src["name"], [(0.0, src["duration"])]))
    djob = dataset.Job(voice=vid, language=lang_of(vid))
    dataset.jobs[vid] = djob
    t = threading.Thread(target=dataset._run, args=(djob, todo, job.separate), daemon=True)
    t.start()
    while t.is_alive():
        t.join(0.5)
        if job.cancel:
            djob.cancel = True
        job.update(stage=f"整理素材 · {djob.stage}", progress=round(0.02 + 0.38 * djob.progress, 3))
    if djob.state == "cancelled" or job.cancel:
        raise _Cancelled()
    if djob.state != "done":
        raise RuntimeError(djob.message or "素材处理失败。")


def _after_material(job: CloneJob, vid: str, fingerprint: str, secs: float) -> None:
    v = voices.get(vid)
    if not job.train or secs < dataset.MIN_TOTAL or not (v.get("weights") or {}).get("sovits"):
        # zero-shot from now on (until a fine-tune finishes)
        voices.set_weights(vid, None, None)
        voices.update(vid, {"base": config.load_settings()["engine"].get("zero_shot_base", "v5turbo")})
    _mark_cloned(vid, fingerprint, "zero-shot", secs)  # usable right away, also if training fails
    mode = "zero-shot"
    # 4. fine-tune with a minute or more of usable speech
    if job.train and secs >= dataset.MIN_TOTAL:
        job.update(phase="train", stage="训练（v2ProPlus）", progress=0.45)
        try:
            training.start(vid, {"base": TRAIN_BASE, "fresh": True})
        except ValueError as e:
            raise RuntimeError(f"{e}（零样本克隆已可用）") from e
        while True:
            time.sleep(1.0)
            tj = training.jobs.get(vid)
            if tj is None:
                break
            if job.cancel:
                training.cancel(vid)
            job.update(stage=f"训练 · {tj.stage_label}", progress=round(0.45 + 0.55 * tj.progress, 3))
            if tj.status != "running":
                break
        tj = training.jobs.get(vid)
        if tj and tj.status == "done":
            mode = "trained"
            _mark_cloned(vid, fingerprint, mode, secs)
        elif job.cancel or (tj and tj.status == "cancelled"):
            raise _Cancelled()
        else:
            raise RuntimeError("训练失败：" + (tj.message if tj else "未知错误") + "（零样本克隆已可用）")
    if load_clips(vid).get("mode") == "omnivoice":
        try:
            _pick_omni_ref(vid)
        except ValueError:
            pass
    job.update(state="done", phase="done", mode=mode, progress=1.0, stage="完成",
               message="已用训练后的声线朗读。" if mode == "trained" else "已完成零样本克隆。")


def _mark_cloned(vid: str, fingerprint: str, mode: str, seconds: float) -> None:
    with _lock:
        cur = load_clips(vid)
        cur["cloned"] = {"at": voices.now_iso(), "fingerprint": fingerprint, "mode": mode, "seconds": seconds}
        _save_clips(vid, cur)


def _keep_main_speaker(vid: str) -> None:
    """Force the folder's language, then drop sentences that sound like someone else:
    the target is the centroid of the half of sentences closest to the folder as a whole."""
    lang = lang_of(vid)
    with dataset._lock:
        d = dataset.load(vid)
        for i in d["items"]:
            i["lang"] = lang
        dataset.save(vid, d)
    items = [i for i in d["items"] if i.get("text") and i.get("embedding")]
    if len(items) < 2:
        return
    E = np.array([i["embedding"] for i in items], np.float32)
    c = E.mean(axis=0)
    c /= np.linalg.norm(c) + 1e-9
    sims = E @ c
    core = [items[k]["id"] for k in np.argsort(-sims)[: max(1, len(items) // 2)]]
    dataset.set_target(vid, core)
    dataset.bulk(vid, action="filter", min_similarity=SAME_SPEAKER - 0.05, min_score=15)


def _pick_ref(vid: str) -> dict[str, Any] | None:
    import shutil

    items = [i for i in dataset.load(vid)["items"] if not i.get("deleted") and i.get("text")]
    if not items:
        return None
    lang = lang_of(vid)
    sure = [i for i in items if not _doubt(i["text"], i["duration"], i.get("asr_alt") or i.get("asr_ja") or None, lang)] or items
    good = [i for i in sure if 3.0 <= i["duration"] <= 10.0] or sure
    best = max(good, key=lambda i: (i["score"], i.get("similarity") or 0))
    ref_dir = voices.folder(vid) / "ref"
    ref_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(dataset.ddir(vid) / "clips" / f"{best['id']}.wav", ref_dir / "ref.wav")
    voices.set_ref(vid, "ref/ref.wav", best["text"], lang_of(vid), best["duration"])
    _tag_ref(vid, best["id"])
    return best


# ------------------------------------------------------------------ transcripts
_LATIN = re.compile(r"[A-Za-z]")
_KANA_KANJI = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


_PUNCT = re.compile(r"[\s、。，,.！!？?…「」『』〜~ー]")
_KANA = re.compile(r"[\u3040-\u30ff]")


def _similar(a: str, b: str) -> float:
    import difflib

    return difflib.SequenceMatcher(None, _PUNCT.sub("", a), _PUNCT.sub("", b)).ratio()


def _doubt(text: str, duration: float, alt: str | None = None, lang: str = LANG) -> str | None:
    """Why a transcript may be wrong, or None. SenseVoice reports no confidence, so the signal is
    a second reading of the same audio with the language forced to the folder's (`alt`): where the
    two readings disagree the recogniser was unsure. Plus plausibility checks per language."""
    t = (text or "").strip()
    if not t:
        return "没有识别出文字"
    if alt is not None and _similar(t.lower(), alt.lower()) < 0.9:
        return "两次识别结果不一致"
    if re.search(r"(.)\1{3,}", t):
        return "同一个字连续重复"
    if lang == "ja":
        if len(t) >= 2 and not _KANA.search(t):
            return "没有假名，可能识别成了中文"
        if re.search(r"[？?！!](?=[\u3040-\u309f])", t):
            return "句中有多余的标点"
        if _LATIN.search(t):
            return "含有字母，可能识别成了别的语言"
        n = len(_KANA_KANJI.findall(t))
        if n < len(t.replace(" ", "")) * 0.6:
            return "日语字符偏少"
        lo, hi = 2.5, 11
    elif lang == "zh":
        if _KANA.search(t):
            return "含有假名，可能识别成了日语"
        han = len(re.findall(r"[\u3400-\u9fff]", t))
        if han < len(re.sub(r"[\s\W]", "", t)) * 0.5:
            return "汉字偏少，可能识别成了别的语言"
        if re.search(r"[？?！!](?=[\u3400-\u9fff])", t):
            return "句中有多余的标点"
        n, lo, hi = han, 1.5, 9
    else:  # en
        if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", t):
            return "含有中日文字，可能识别成了别的语言"
        n, lo, hi = len(re.findall(r"[A-Za-z']+", t)), 0.8, 5.5
    rate = n / max(duration, 0.1)
    if rate < lo:
        return "文字比声音短很多，可能漏字"
    if rate > hi:
        return "文字比声音长很多，可能多字"
    return None


def _second_opinion(vid: str) -> dict[str, str]:
    """Transcript of every sentence with the language forced to the folder's (computed once, kept
    in items.json as asr_alt with the language it was made for)."""
    lang = lang_of(vid)

    def alt(i):  # sentences checked before languages were per folder carry asr_ja
        if i.get("asr_alt_lang") == lang:
            return i.get("asr_alt")
        return i.get("asr_ja") if lang == "ja" and "asr_ja" in i else None

    with dataset._lock:
        d = dataset.load(vid)
        todo = [i for i in d["items"] if alt(i) is None]
    got = {}
    for i in todo:
        try:
            x = audio_util.decode(dataset.ddir(vid) / "clips" / f"{i['id']}.wav", 16000)
            got[i["id"]] = asr.transcribe(x, lang)["text"]
        except Exception as e:  # missing clip or ASR model: no second opinion for this one
            log.info("second transcript failed for %s: %s", i["id"], e)
    if got:
        with dataset._lock:
            d = dataset.load(vid)
            for i in d["items"]:
                if i["id"] in got:
                    i["asr_alt"], i["asr_alt_lang"] = got[i["id"]], lang
            dataset.save(vid, d)
    return {i["id"]: alt(i) or "" for i in dataset.load(vid)["items"]}


def transcripts(vid: str) -> dict[str, Any]:
    v = voices.get(vid)
    if v.get("ref") and not v["ref"].get("item"):  # folders cloned before the link was recorded
        same = [i for i in dataset.load(vid)["items"] if i.get("text") == v["ref"]["text"]]
        if same:
            _tag_ref(vid, same[0]["id"])
            v = voices.get(vid)
    alt = _second_opinion(vid)
    lang = lang_of(vid)
    items = []
    for i in dataset.load(vid)["items"]:
        a = alt.get(i["id"]) or None
        text = i.get("text", "")
        items.append({"id": i["id"], "text": text, "asr_text": i.get("asr_text", ""), "duration": i["duration"],
                      "deleted": bool(i.get("deleted")), "score": i.get("score"), "similarity": i.get("similarity"),
                      "edited": text != i.get("asr_text", ""),
                      "suggest": a if a and _PUNCT.sub("", a) != _PUNCT.sub("", text) else None,
                      # once the user has edited a sentence it is theirs: no more warnings
                      "doubt": None if i.get("deleted") or text != i.get("asr_text", "") else _doubt(text, i["duration"], a, lang)})
    ref = v.get("ref")
    ref_item = next((i for i in items if ref and i["id"] == ref.get("item")), None)
    return {
        "ref": {**ref, "doubt": ref_item["doubt"] if ref_item else _doubt(ref["text"], ref["duration"], None, lang),
                "suggest": ref_item["suggest"] if ref_item else None} if ref else None,
        "items": items,
        "doubtful": sum(1 for i in items if i["doubt"]),
    }


def _mark_texts_edited(vid: str) -> None:
    with _lock:
        data = load_clips(vid)
        data["texts_edited"] = voices.now_iso()
        _save_clips(vid, data)


def edit_transcript(vid: str, item_id: str, text: str | None = None, deleted: bool | None = None) -> dict[str, Any]:
    patch = {}
    if text is not None:
        patch["text"] = text
    if deleted is not None:
        patch["deleted"] = deleted
    item = dataset.edit_item(vid, item_id, patch)
    v = voices.get(vid)
    # the reference was copied from this sentence: keep its text in step
    if text is not None and v.get("ref") and v["ref"].get("item") == item_id:
        voices.set_ref(vid, v["ref"]["audio"], item["text"], lang_of(vid), v["ref"]["duration"])
        _tag_ref(vid, item_id)
    _mark_texts_edited(vid)
    return item


def set_ref_text(vid: str, text: str) -> dict[str, Any]:
    v = voices.get(vid)
    if not v.get("ref"):
        raise ValueError("还没有参考音频，请先克隆。")
    if not text.strip():
        raise ValueError("参考文字不能为空。")
    item = v["ref"].get("item")
    voices.set_ref(vid, v["ref"]["audio"], text, lang_of(vid), v["ref"]["duration"])
    if item:
        _tag_ref(vid, item)
        try:
            dataset.edit_item(vid, item, {"text": text})
            _mark_texts_edited(vid)
        except KeyError:
            pass
    return voices.get(vid)["ref"]


def use_as_ref(vid: str, item_id: str) -> dict[str, Any]:
    import shutil

    it = next((i for i in dataset.load(vid)["items"] if i["id"] == item_id), None)
    if not it or not it.get("text"):
        raise ValueError("这句没有文字，不能作为参考。")
    ref_dir = voices.folder(vid) / "ref"
    ref_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(dataset.ddir(vid) / "clips" / f"{item_id}.wav", ref_dir / "ref.wav")
    voices.set_ref(vid, "ref/ref.wav", it["text"], lang_of(vid), it["duration"])
    _tag_ref(vid, item_id)
    return voices.get(vid)["ref"]


def _tag_ref(vid: str, item_id: str) -> None:
    """Remember which sentence the reference came from (voice.json, next to the ref)."""
    p = voices.folder(vid) / "voice.json"
    v = json.loads(p.read_text(encoding="utf-8"))
    if v.get("ref"):
        v["ref"]["item"] = item_id
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(v, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)


# ------------------------------------------------------------------ cleaner training set
CLEAN_MAX_BGM_DB = -10.0  # drop sentences whose accompaniment was this loud before separation


def _wiener(x: np.ndarray, sr: int, max_cut_db: float = 10.0) -> np.ndarray:
    """Gentle stationary-noise reduction: Wiener gain from a noise profile taken from the quietest
    frames, limited to max_cut_db so breaths and fricatives (well above the floor) pass untouched."""
    from scipy.signal import istft, stft

    f, t, Z = stft(x, sr, nperseg=1024, noverlap=768)
    P = np.abs(Z) ** 2
    e = P.sum(0)
    q = e <= np.percentile(e, 10)
    noise = P[:, q].mean(1, keepdims=True) + 1e-14
    snr = np.maximum(P / noise - 1.0, 0.0)
    g = np.maximum(snr / (snr + 1.0), 10 ** (-max_cut_db / 20))
    _, y = istft(Z * g, sr, nperseg=1024, noverlap=768)
    return np.asarray(y[: len(x)], np.float32)


def build_clean_set(vid: str) -> dict[str, Any]:
    """A cleaner copy of the folder's sentences for training (dataset/clean/, originals untouched):
    sentences that had heavy accompaniment before separation are left out, and the rest get a
    gentle noise reduction where their floor is not already far below the voice."""
    from scipy.signal import stft

    out = dataset.ddir(vid) / "clean"
    out.mkdir(parents=True, exist_ok=True)
    keep, dropped, denoised = [], [], 0
    for i in dataset.load(vid)["items"]:
        if i.get("deleted") or not i.get("text"):
            continue
        if i["quality"].get("bgm_db", -99) > CLEAN_MAX_BGM_DB:
            dropped.append(i["id"])
            continue
        x, sr = sf.read(str(dataset.ddir(vid) / "clips" / f"{i['id']}.wav"), dtype="float32")
        _, _, Z = stft(x, sr, nperseg=1024, noverlap=768)
        db = 10 * np.log10((np.abs(Z) ** 2).sum(0) + 1e-12)
        if np.percentile(db, 10) - np.percentile(db, 95) > -50:  # audible floor: clean it
            x = _wiener(x, sr)
            denoised += 1
        sf.write(str(out / f"{i['id']}.wav"), np.clip(x, -1, 1), sr, subtype="PCM_16")
        keep.append(i["id"])
    secs = round(sum(i["duration"] for i in dataset.load(vid)["items"] if i["id"] in set(keep)), 1)
    info = {"ids": keep, "dropped": dropped, "denoised": denoised, "seconds": secs, "max_bgm_db": CLEAN_MAX_BGM_DB}
    (out / "manifest.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    return info


# ------------------------------------------------------------------ model versions
def _weights_dir(vid: str) -> Path:
    return voices.folder(vid) / "weights"


def models(vid: str) -> dict[str, Any]:
    data = load_clips(vid)
    return {"models": data.get("models", []), "active": data.get("model")}


def archive_current(vid: str, label: str, note: str = "") -> dict[str, Any] | None:
    """Move the voice's current fine-tuned weights into weights/versions/<id>/ (the training run
    that follows only cleans up its own output folders) and list them as a selectable version."""
    import shutil

    v = voices.get(vid)
    w = v.get("weights") or {}
    if not (w.get("gpt") and w.get("sovits")):
        return None
    with _lock:
        data = load_clips(vid)
        ms = data.setdefault("models", [])
        known = next((m for m in ms if m["gpt"] == w["gpt"] and m["sovits"] == w["sovits"]), None)
        if known:
            return known
        mid = f"v{len(ms) + 1}"
        dst = _weights_dir(vid) / "versions" / mid
        dst.mkdir(parents=True, exist_ok=True)
        rel = {}
        for k in ("gpt", "sovits"):
            src = voices.folder(vid) / w[k]
            shutil.move(str(src), str(dst / src.name))
            rel[k] = f"weights/versions/{mid}/{src.name}"
        m = {"id": mid, "label": label, "note": note, "gpt": rel["gpt"], "sovits": rel["sovits"],
             "base": v.get("base"), "at": w.get("trained_at") or voices.now_iso()}
        ms.append(m)
        data["model"] = mid
        _save_clips(vid, data)
    voices.set_weights(vid, m["gpt"], m["sovits"])
    return m


def use_model(vid: str, mid: str) -> dict[str, Any]:
    data = load_clips(vid)
    m = next((m for m in data.get("models", []) if m["id"] == mid), None)
    if not m:
        raise KeyError(mid)
    voices.set_weights(vid, m["gpt"], m["sovits"])
    if m.get("base"):
        voices.update(vid, {"base": m["base"]})
    with _lock:
        data = load_clips(vid)
        data["model"] = mid
        _save_clips(vid, data)
    return m


def _keep_only(vid: str, mid: str) -> None:
    """Keep just the model in use: other versions and the per-epoch checkpoints are deleted
    (the comparison audio under train/compare stays for listening)."""
    import shutil

    with _lock:
        data = load_clips(vid)
        keep = next(m for m in data.get("models", []) if m["id"] == mid)
        used = {(voices.folder(vid) / keep["gpt"]).resolve(), (voices.folder(vid) / keep["sovits"]).resolve()}
        for f in list((_weights_dir(vid) / "versions").rglob("*")):
            if f.is_file() and f.resolve() not in used:
                f.unlink()
        for d in sorted((_weights_dir(vid) / "versions").glob("*"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                shutil.rmtree(d, ignore_errors=True)
        keep.pop("epochs", None)
        data["models"] = [keep]
        _save_clips(vid, data)


# ------------------------------------------------------------------ retrain for less noise
RETRAIN_S2_EPOCHS = 8   # every epoch is kept and measured; the previous run used 10
COMPARE = {"sib": "静かな森の中で、少女はそっと空を見上げました。", "plain": "すみません、新しい小説はどこで探せますか。"}


def _fricative_flatness(x: np.ndarray, sr: int) -> float:
    from scipy.signal import stft

    f, t, Z = stft(x, sr, nperseg=1024, noverlap=768)
    P = np.abs(Z) ** 2
    tot = P.sum(0) + 1e-12
    band = P[(f >= 4000) & (f <= 11000)]
    db = 10 * np.log10(tot)
    sel = (band.sum(0) / tot > 0.5) & (db > np.percentile(db, 30))
    if sel.sum() < 3:
        return float("nan")
    hb = band[:, sel] + 1e-12
    return float(np.mean(np.exp(np.mean(np.log(hb), 0)) / np.mean(hb, 0)))


def _hiss_db(x: np.ndarray, sr: int) -> float:
    """Energy above 11 kHz in voiced, non-fricative frames relative to the frame (dB): the
    noise that rides on the voice."""
    from scipy.signal import stft

    f, t, Z = stft(x, sr, nperseg=1024, noverlap=768)
    P = np.abs(Z) ** 2
    tot = P.sum(0) + 1e-12
    db = 10 * np.log10(tot)
    voiced = (db > np.percentile(db, 95) - 20) & (P[(f >= 4000) & (f <= 11000)].sum(0) / tot < 0.2)
    if voiced.sum() < 3:
        return float("nan")
    return float(10 * np.log10(P[f >= 11000][:, voiced].sum() / P[:, voiced].sum() + 1e-12))


async def _render(vid: str, text: str, gpt: str, sovits: str) -> tuple[np.ndarray, int]:
    from dataclasses import replace

    from . import speech
    from .engine import engine

    v = voices.get(vid)
    req = speech.build_request(v, text, text_lang(text, lang_of(vid)), 1.0, 0)
    req = replace(req, gpt_weights=str((voices.folder(vid) / gpt).resolve()),
                  sovits_weights=str((voices.folder(vid) / sovits).resolve()), seed=20261027)
    chunks, sr = [], 32000
    async for sr, pcm in engine.synthesize(req, presence=False):
        chunks.append(pcm)
    return np.concatenate(chunks).astype(np.float32) / 32768.0, sr


async def compare_models(vid: str, entries: list[dict[str, str]], out_dir: Path) -> list[dict[str, Any]]:
    """Render the comparison sentences with each (gpt, sovits) pair (fixed seed) and measure
    fricative flatness, hiss on the voice and similarity to the folder's speaker."""
    d = dataset.load(vid)
    E = np.array([i["embedding"] for i in d["items"] if not i.get("deleted") and i.get("embedding")], np.float32)
    centre = E.mean(0)
    centre /= np.linalg.norm(centre) + 1e-9
    out_dir.mkdir(parents=True, exist_ok=True)
    res = []
    for e in entries:
        fl, hs, sim = [], [], []
        for key, text in COMPARE.items():
            x, sr = await _render(vid, text, e["gpt"], e["sovits"])
            sf.write(str(out_dir / f"{key}_{e['name']}.wav"), x, sr, subtype="PCM_16")
            fl.append(_fricative_flatness(x, sr))
            hs.append(_hiss_db(x, sr))
            emb = _embed(audio_util.decode(out_dir / f"{key}_{e['name']}.wav", 16000))
            sim.append(float(emb @ centre) if emb is not None else float("nan"))
        res.append({**e, "flatness": round(float(np.nanmean(fl)), 3), "hiss_db": round(float(np.nanmean(hs)), 1),
                    "similarity": round(float(np.nanmean(sim)), 3)})
    return res


def start_retrain(vid: str) -> "CloneJob":
    """Retrain the folder's voice for less noise: archive the current model as a version, train on
    the cleaned set keeping every SoVITS epoch, then render and measure each epoch against the
    previous model, switch to the best balance of noise and likeness and keep only that one."""
    voices.get(vid)
    with _lock:
        j = jobs.get(vid)
        if j and j.state == "running":
            raise ValueError("这个文件夹正在学习。")
        if not dataset.load(vid)["items"]:
            raise ValueError("文件夹还没有整理好的句子，请先点“克隆这个声线”。")
        job = CloneJob(voice=vid, train=True, separate=False)
        job.phase = "train"
        jobs[vid] = job
    threading.Thread(target=_retrain, args=(job,), daemon=True, name=f"retrain-{vid}").start()
    return job


def _retrain(job: "CloneJob") -> None:
    import asyncio
    import shutil

    vid = job.voice
    loop = bus._loop

    def on_loop(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=900)

    try:
        prev = archive_current(vid, "第 1 版", "原训练（声码器 10 轮）")
        job.update(stage="整理干净的训练集", progress=0.02)
        clean = build_clean_set(vid)
        if clean["seconds"] < dataset.MIN_TOTAL:
            raise RuntimeError(f"剔除伴奏重的句子后只剩 {clean['seconds']:.0f} 秒，不够训练（至少 60 秒）。")
        job.update(stage=f"训练（干净素材 {clean['seconds']:.0f} 秒，声码器 {RETRAIN_S2_EPOCHS} 轮）", progress=0.05)
        try:
            training.start(vid, {"base": TRAIN_BASE, "fresh": True, "clean": True, "keep_s2": True,
                                 "s2_epochs": RETRAIN_S2_EPOCHS})
        except ValueError as e:
            raise RuntimeError(str(e)) from e
        while True:
            time.sleep(1.0)
            tj = training.jobs.get(vid)
            if job.cancel:
                training.cancel(vid)
            if tj is None:
                break
            job.update(stage=f"训练 · {tj.stage_label}", progress=round(0.05 + 0.75 * tj.progress, 3))
            if tj.status != "running":
                break
        tj = training.jobs.get(vid)
        if not tj or tj.status != "done":
            if prev:
                use_model(vid, prev["id"])
            if job.cancel or (tj and tj.status == "cancelled"):
                raise _Cancelled()
            raise RuntimeError("训练失败：" + (tj.message if tj else "未知错误") + "（已换回第 1 版）")
        # move the new run (final + per-epoch SoVITS) into versions/<id>/
        v = voices.get(vid)
        w = v["weights"]
        with _lock:
            data = load_clips(vid)
            mid = f"v{len(data.get('models', [])) + 1}"
        dst = _weights_dir(vid) / "versions" / mid
        dst.mkdir(parents=True, exist_ok=True)
        gpt = dst / Path(w["gpt"]).name
        shutil.move(str(voices.folder(vid) / w["gpt"]), str(gpt))
        sovits_all = sorted((_weights_dir(vid) / "sovits" / "epochs").glob("*.pth")) + [voices.folder(vid) / w["sovits"]]
        for f in sovits_all:
            shutil.move(str(f), str(dst / f.name))
        shutil.rmtree(_weights_dir(vid) / "sovits" / "epochs", ignore_errors=True)
        rel = lambda f: str(Path(f).resolve().relative_to(voices.folder(vid).resolve())).replace("\\", "/")
        entries = []
        if prev:
            entries.append({"name": "v1", "gpt": prev["gpt"], "sovits": prev["sovits"]})
        for f in sorted(dst.glob("*.pth"), key=lambda f: int(re.search(r"_e(\d+)_", f.name).group(1))):
            ep = int(re.search(r"_e(\d+)_", f.name).group(1))
            entries.append({"name": f"{mid}-e{ep}", "gpt": rel(gpt), "sovits": rel(f), "epoch": ep})
        job.update(stage=f"逐轮试听比较（{len(entries)} 个模型）", progress=0.82)
        res = on_loop(compare_models(vid, entries, voices.folder(vid) / "train" / "compare"))
        # pick: the least hissy epoch that keeps at least 97 % of the previous model's likeness
        base_sim = res[0]["similarity"] if prev else max(r["similarity"] for r in res)
        # pick among the previous model and every new epoch: the least "electronic" fricatives that
        # keep at least 97 % of the previous model's likeness; only the winner is kept
        cands = [r for r in res if r["similarity"] >= 0.97 * base_sim] or res
        best = min(cands, key=lambda r: (r["flatness"], -r["similarity"]))
        with _lock:
            data = load_clips(vid)
            data["retrain_report"] = {"clean": clean, "results": res, "picked": best["name"]}
            if best.get("epoch"):
                data.setdefault("models", []).append({
                    "id": mid, "label": f"第 {len(data['models']) + 1} 版", "note": f"干净素材 {clean['seconds']:.0f} 秒，声码器第 {best['epoch']} 轮",
                    "gpt": best["gpt"], "sovits": best["sovits"], "base": TRAIN_BASE, "at": voices.now_iso(),
                })
            _save_clips(vid, data)
        keep = mid if best.get("epoch") or not prev else prev["id"]
        use_model(vid, keep)
        _keep_only(vid, keep)
        if best.get("epoch"):
            msg = f"已换成新模型（声码器第 {best['epoch']} 轮：嘶声 {best['hiss_db']} dB，像度 {best['similarity']}）。"
        else:
            msg = "新训练的各轮都没有比原来的模型更好，保留原模型。"
        job.update(state="done", phase="done", mode="trained", progress=1.0, stage="完成", message=msg)
    except _Cancelled:
        job.update(state="cancelled", stage="已取消", message="已取消。")
    except Exception as e:
        log.exception("retrain failed")
        job.update(state="error", stage="出错", message=str(e))


# ------------------------------------------------------------------ export a whole article
_SENT = re.compile(r"(?<=[。！？!?\n])|(?<=[.;])\s+")


def chunks(text: str, limit: int = 1200) -> list[str]:
    out, cur = [], ""
    for s in _SENT.split(text):
        if len(cur) + len(s) > limit and cur:
            out.append(cur)
            cur = ""
        while len(s) > limit:
            out.append(s[:limit])
            s = s[limit:]
        cur += s
    if cur.strip():
        out.append(cur)
    return [c for c in (x.strip() for x in out) if c]


async def warm(vid: str) -> None:
    """Load the folder's weights and run one short sentence, so the first read starts quickly
    (a freshly trained voice otherwise pays weight loading and first-inference set-up)."""
    from . import speech

    try:
        job = speech.start(WARM_TEXT[lang_of(vid)], vid, None, play=False, naturalness=False)
        await job.done.wait()
    except Exception as e:  # engine stopped or busy training: nothing to warm
        log.info("warm-up of %s skipped: %s", vid, e)


async def export(vid: str, text: str, fmt: str = "wav", speed: float | None = None, language: str | None = None) -> dict[str, Any]:
    from . import compliance, speech

    fmt = fmt if fmt in ("wav", "mp3") else "wav"
    v = voices.get(vid)
    parts, sr = [], 0
    for c in chunks(text):
        job = speech.start(c, vid, language, play=False, speed=speed or read_speed(vid))
        await job.done.wait()
        if job.error:
            raise RuntimeError(job.error)
        sr = job.sr
        parts += [job.audio(), np.zeros(int(0.5 * sr), np.int16)]
    if not parts:
        raise ValueError("要朗读的文字不能为空。")
    audio = compliance.finalize(np.concatenate(parts[:-1]), sr)
    data, _ = await asyncio.to_thread(compliance.encode_tagged, audio, sr, fmt, v)
    export_dir().mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]+', "_", v["name"])[:40] or vid
    out = export_dir() / f"{safe}-{time.strftime('%Y%m%d-%H%M%S')}.{fmt}"
    out.write_bytes(data)
    return {"path": str(out.resolve()), "seconds": round((len(audio)) / sr, 1), "format": fmt}
