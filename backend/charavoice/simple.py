"""Simple mode: material folders for cloning one person's Japanese voice.

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
LANG = "ja"
REC_VOICE = "__simple__"  # Recording.voice for hotkey recordings (not a real voice)
SAME_SPEAKER = 0.5  # CAM++ cosine: same person usually 0.6-0.85, different people below 0.4
CLIP_GAP = 0.3  # seconds of silence between kept ranges inside one saved clip
TRAIN_BASE = "v2ProPlus"
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
        "dirty": bool(cloned) and cloned.get("fingerprint") != _fingerprint(data["clips"]),
        "job": j.public() if j else None,
        "path": str(clip_dir(vid).resolve()),
        "est": {"zero_shot": est_minutes(secs, False), "train": est_minutes(secs, True)},
    }


def folders() -> list[dict[str, Any]]:
    return [summary(v) for v in voices.list_all() if is_folder(v)]


def create_folder(name: str) -> dict[str, Any]:
    if not consent():
        raise ValueError("请先确认只录制、克隆本人或已获授权的声音。")
    v = voices.create(name, "authorized", LANG)
    v = voices.update(v["id"], {"folder": True})
    clip_dir(v["id"]).mkdir(parents=True, exist_ok=True)
    _save_clips(v["id"], {"clips": [], "cloned": None})
    return summary(v)


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
        # 1. material pipeline over every clip of the folder (fresh each time)
        job.update(stage="准备素材", progress=0.01)
        dataset.clear(vid)
        todo = []
        for c in data["clips"]:
            src = dataset.add_source(vid, c["name"] or c["id"], raw=(clip_dir(vid) / c["file"]).read_bytes())
            todo.append((src["id"], src["name"], [(0.0, src["duration"])]))
        djob = dataset.Job(voice=vid)
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
        # 2. Japanese only; keep the folder's main speaker
        job.update(stage="筛选说话人", progress=0.41)
        _keep_main_speaker(vid)
        # 3. zero-shot reference: the cleanest 3-10 s sentence
        ref = _pick_ref(vid)
        if not ref:
            raise RuntimeError("没有识别出可用的日语语句。请确认片段里是清晰的人声。")
        secs = dataset.summary(vid)["seconds"]
        job.seconds = secs
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
        job.update(state="done", phase="done", mode=mode, progress=1.0, stage="完成",
                   message="已用训练后的声线朗读。" if mode == "trained" else "已完成零样本克隆。")
    except _Cancelled:
        job.update(state="cancelled", stage="已取消", message="已取消。" + ("零样本克隆已可用。" if job.phase == "train" else ""))
    except Exception as e:
        log.exception("clone failed")
        job.update(state="error", stage="出错", message=str(e))


def _mark_cloned(vid: str, fingerprint: str, mode: str, seconds: float) -> None:
    with _lock:
        cur = load_clips(vid)
        cur["cloned"] = {"at": voices.now_iso(), "fingerprint": fingerprint, "mode": mode, "seconds": seconds}
        _save_clips(vid, cur)


def _keep_main_speaker(vid: str) -> None:
    """Force the language to Japanese, then drop sentences that sound like someone else:
    the target is the centroid of the half of sentences closest to the folder as a whole."""
    with dataset._lock:
        d = dataset.load(vid)
        for i in d["items"]:
            i["lang"] = LANG
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
    good = [i for i in items if 3.0 <= i["duration"] <= 10.0] or items
    best = max(good, key=lambda i: (i["score"], i.get("similarity") or 0))
    ref_dir = voices.folder(vid) / "ref"
    ref_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(dataset.ddir(vid) / "clips" / f"{best['id']}.wav", ref_dir / "ref.wav")
    voices.set_ref(vid, "ref/ref.wav", best["text"], LANG, best["duration"])
    return best


# ------------------------------------------------------------------ export a whole article
_SENT = re.compile(r"(?<=[。！？!?\n])")


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


async def export(vid: str, text: str, fmt: str = "wav") -> dict[str, Any]:
    from . import compliance, speech

    fmt = fmt if fmt in ("wav", "mp3") else "wav"
    v = voices.get(vid)
    parts, sr = [], 0
    for c in chunks(text):
        job = speech.start(c, vid, LANG, play=False)
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
