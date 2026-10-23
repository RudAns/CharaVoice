"""Training material pipeline for one voice.

    import (ffmpeg) -> vocal separation (GPT-SoVITS UVR5, optional) -> Silero VAD
    -> 2-10 s clips -> SenseVoice transcript -> quality score -> speaker similarity
    -> review (edit text / delete / threshold) -> GPT-SoVITS training list

Layout under data/voices/<id>/dataset/:
    sources/<src>.<ext>        imported files as uploaded
    work/<src>_44k.wav         44.1 kHz stereo decode; work/vocal, work/other from UVR5
    clips/<src>_<n>.wav        32 kHz mono 16-bit clips
    items.json                 clip metadata and review state
    train.list                 export: wav|speaker|LANG|text (GPT-SoVITS format)
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from . import asr, audio_util, config, procutil, voices
from .events import bus

log = logging.getLogger("charavoice.dataset")
SPEAKER_MODEL = "models/speaker/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
UVR_MODEL = "model_bs_roformer_ep_317_sdr_12.9755"
MIN_TOTAL, RECOMMENDED_TOTAL = 60.0, 600.0
CLIP_MIN, CLIP_MAX = 2.0, 10.0
_lock = threading.RLock()
_extractor = None


# ------------------------------------------------------------------ storage
def ddir(vid: str) -> Path:
    return voices.folder(vid) / "dataset"


def _items_path(vid: str) -> Path:
    return ddir(vid) / "items.json"


def load(vid: str) -> dict[str, Any]:
    p = _items_path(vid)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"items": [], "target": None, "sources": []}


def save(vid: str, data: dict[str, Any]) -> None:
    p = _items_path(vid)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def summary(vid: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = data or load(vid)
    kept = [i for i in data["items"] if not i.get("deleted")]
    usable = [i for i in kept if i.get("text")]
    total = round(sum(i["duration"] for i in usable), 1)
    langs: dict[str, float] = {}
    for i in usable:
        langs[i["lang"]] = round(langs.get(i["lang"], 0) + i["duration"], 1)
    return {
        "clips": len(data["items"]),
        "kept": len(kept),
        "usable": len(usable),
        "seconds": total,
        "min_seconds": MIN_TOTAL,
        "recommended_seconds": RECOMMENDED_TOTAL,
        "level": "recommended" if total >= RECOMMENDED_TOTAL else "minimum" if total >= MIN_TOTAL else "insufficient",
        "languages": langs,
        "has_target": bool(data.get("target")),
    }


# ------------------------------------------------------------------ jobs
@dataclass
class Job:
    voice: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    state: str = "running"  # running | done | error | cancelled
    stage: str = "排队中"
    progress: float = 0.0
    message: str = ""
    started: float = field(default_factory=time.time)
    cancel: bool = False

    def update(self, stage: str | None = None, progress: float | None = None, message: str | None = None) -> None:
        if stage is not None:
            self.stage = stage
        if progress is not None:
            self.progress = round(min(1.0, max(0.0, progress)), 3)
        if message is not None:
            self.message = message
        bus.publish("dataset_job", **self.public())

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("cancel")
        return d


jobs: dict[str, Job] = {}


def current_job(vid: str) -> Job | None:
    j = jobs.get(vid)
    return j


# ------------------------------------------------------------------ sources (pending trim)
def _source(data: dict[str, Any], sid: str) -> dict[str, Any]:
    for src in data.get("sources", []):
        if src["id"] == sid:
            return src
    raise KeyError(sid)


def work_wav(vid: str, sid: str) -> Path:
    return ddir(vid) / "work" / f"{sid}_44k.wav"


def add_source(vid: str, name: str, raw: bytes | None = None, path: Path | None = None, origin: str = "file") -> dict[str, Any]:
    """Store an uploaded file (or a finished recording) and decode it for the trim editor."""
    sid = uuid.uuid4().hex[:6]
    src_dir = ddir(vid) / "sources"
    src_dir.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path = src_dir / f"{sid}{Path(name).suffix.lower() or '.bin'}"
        path.write_bytes(raw)
    assert path is not None
    stereo = _decode_stereo(path)
    if len(stereo) < 44100:
        raise ValueError(f"“{name}”太短或没有音轨（至少 1 秒）。")
    w44 = work_wav(vid, sid)
    w44.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(w44), stereo, 44100, subtype="PCM_16")
    src = {
        "id": sid, "name": name, "origin": origin, "file": path.name,
        "duration": round(len(stereo) / 44100, 2), "status": "pending",
        "added_at": voices.now_iso(), "kept": None, "separated": None,
    }
    with _lock:
        data = load(vid)
        data.setdefault("sources", []).append(src)
        save(vid, data)
    bus.publish("dataset_changed", voice=vid)
    return src


def peaks(vid: str, sid: str, per_second: int = 100) -> dict[str, Any]:
    """Min/max envelope of the mono mix for drawing the waveform."""
    x, sr = sf.read(str(work_wav(vid, sid)), dtype="float32", always_2d=True)
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


def remove_source(vid: str, sid: str) -> None:
    with _lock:
        data = load(vid)
        src = _source(data, sid)
        data["sources"] = [s for s in data["sources"] if s["id"] != sid]
        data["items"] = [i for i in data["items"] if i["source"] != sid]
        save(vid, data)
    for p in [work_wav(vid, sid), ddir(vid) / "sources" / src.get("file", "")]:
        if p.is_file():
            p.unlink()
    for p in (ddir(vid) / "clips").glob(f"{sid}_*.wav"):
        p.unlink()
    bus.publish("dataset_changed", voice=vid)


def normalize_ranges(ranges: list[list[float]], duration: float) -> list[tuple[float, float]]:
    out = sorted((max(0.0, float(a)), min(duration, float(b))) for a, b in ranges if float(b) - float(a) > 0.05)
    merged: list[list[float]] = []
    for a, b in out:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(round(a, 3), round(b, 3)) for a, b in merged]


def _busy(vid: str) -> None:
    j = jobs.get(vid)
    if j and j.state == "running":
        raise ValueError("这个声音已经有一个素材处理任务在运行，请等它完成。")


def start_process(vid: str, sid: str, ranges: list[list[float]] | None, separate: bool) -> Job:
    with _lock:
        _busy(vid)
        src = _source(load(vid), sid)
        kept = normalize_ranges(ranges or [[0, src["duration"]]], src["duration"])
        if not kept:
            raise ValueError("没有保留任何片段。")
        job = Job(voice=vid)
        jobs[vid] = job
    threading.Thread(target=_run, args=(job, [(sid, src["name"], kept)], separate), daemon=True, name=f"dataset-{vid}").start()
    return job


def start_import(vid: str, files: list[tuple[str, bytes]], separate: bool) -> Job:
    """Import and process whole files in one step (API / scripts, no trimming)."""
    _busy(vid)
    todo = []
    for name, raw in files:
        if raw:
            src = add_source(vid, name, raw=raw)
            todo.append((src["id"], name, [(0.0, src["duration"])]))
    if not todo:
        raise ValueError("没有收到有效的文件。")
    job = Job(voice=vid)
    jobs[vid] = job
    threading.Thread(target=_run, args=(job, todo, separate), daemon=True, name=f"dataset-{vid}").start()
    return job


def cancel(vid: str) -> bool:
    j = jobs.get(vid)
    if j and j.state == "running":
        j.cancel = True
        return True
    return False


class Cancelled(Exception):
    pass


def _run(job: Job, todo: list[tuple[str, str, list[tuple[float, float]]]], separate: bool) -> None:
    vid = job.voice
    try:
        n = len(todo)
        for k, (sid, name, kept) in enumerate(todo):
            base, span = k / n, 1 / n

            def step(frac: float, stage: str, _b=base, _s=span, _name=name):
                if job.cancel:
                    raise Cancelled()
                job.update(stage=f"{_name}：{stage}", progress=_b + _s * frac)

            _process_source(vid, sid, kept, separate, step)
        job.state = "done"
        s = summary(vid)
        job.update(stage="完成", progress=1.0, message=f"共 {s['usable']} 段可用，合计 {s['seconds']:.0f} 秒")
    except Cancelled:
        job.state = "cancelled"
        job.update(stage="已取消")
    except Exception as e:
        log.exception("dataset job failed")
        job.state = "error"
        job.update(stage="出错", message=str(e))


GAP = 1.0  # seconds of silence between kept ranges in the trimmed working file


def _process_source(vid: str, sid: str, kept: list[tuple[float, float]], separate: bool, step) -> None:
    work = ddir(vid) / "work"
    clips = ddir(vid) / "clips"
    clips.mkdir(parents=True, exist_ok=True)

    step(0.02, "准备保留片段")
    full, sr = sf.read(str(work_wav(vid, sid)), dtype="float32", always_2d=True)
    pieces, offsets, pos = [], [], 0  # offsets: (start in trimmed file, start in original, length), seconds
    for a, b in kept:
        seg = full[int(a * sr) : int(b * sr)]
        offsets.append((pos / sr, a, len(seg) / sr))
        pieces += [seg, np.zeros((int(GAP * sr), full.shape[1]), np.float32)]
        pos += len(seg) + int(GAP * sr)
    tw = work / f"{sid}_trim.wav"
    sf.write(str(tw), np.concatenate(pieces), sr, subtype="PCM_16")
    orig16 = audio_util.decode(tw, 16000)

    vocal_path, other_path = tw, None
    if separate:
        step(0.08, "人声分离（BS-Roformer）")
        vocal_path, other_path = _separate(tw, work)
    step(0.45, "切分句子")
    v16 = audio_util.decode(vocal_path, 16000)
    v32 = audio_util.decode(vocal_path, 32000)
    o16 = audio_util.decode(other_path, 16000) if other_path else None
    segs = _segments(v16)

    def to_original(t: float) -> float:
        """Map a time in the trimmed file back to the source; times inside a padding gap
        snap to the nearer kept edge (end of this range or start of the next)."""
        for k, (ts, os_, ln) in enumerate(offsets):
            if ts <= t <= ts + ln:
                return os_ + (t - ts)
            if ts + ln < t < ts + ln + GAP:
                if k + 1 < len(offsets) and t - (ts + ln) > GAP / 2:
                    return offsets[k + 1][1]
                return os_ + ln
        return t

    data = load(vid)
    target = np.array(data["target"]["embedding"], dtype=np.float32) if data.get("target") else _voice_ref_embedding(vid)
    new_items = []
    for i, (a, b) in enumerate(segs):
        step(0.5 + 0.48 * i / max(1, len(segs)), f"转写与打分 {i + 1}/{len(segs)}")
        seg16 = v16[a:b]
        cid = f"{sid}_{i:04d}"
        sf.write(str(clips / f"{cid}.wav"), np.clip(v32[a * 2 : b * 2], -1, 1), 32000, subtype="PCM_16")
        r = asr.transcribe(seg16)
        q = _quality(seg16, orig16[a:b], o16[a:b] if o16 is not None else None)
        emb = _embed(seg16)
        sim = float(np.dot(emb, target)) if target is not None else None
        new_items.append({
            "id": cid,
            "source": sid,
            "start": round(to_original(a / 16000), 2),
            "end": round(to_original(b / 16000), 2),
            "duration": round((b - a) / 16000, 2),
            "text": r["text"],
            "asr_text": r["text"],
            "lang": r["language"] if r["language"] else "zh",
            "emotion": r["emotion"],
            "quality": q,
            "score": _score(q, (b - a) / 16000, bool(r["text"])),
            "similarity": round(sim, 3) if sim is not None else None,
            "embedding": [round(float(v), 5) for v in emb],
            "deleted": False,
        })
    with _lock:
        cur = load(vid)
        cur["items"] = [i for i in cur["items"] if i["source"] != sid] + new_items
        src = _source(cur, sid)
        src.update(status="processed", kept=[list(k) for k in kept], separated=separate, processed_at=voices.now_iso())
        if target is not None and not cur.get("target"):
            cur["target"] = {"source": "ref", "embedding": target.tolist()}
        save(vid, cur)
    tw.unlink(missing_ok=True)
    bus.publish("dataset_changed", voice=vid)


def _decode_stereo(path: Path) -> np.ndarray:
    import subprocess

    cmd = [audio_util.ffmpeg_exe(), "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "2", "-ar", "44100", "-f", "f32le", "pipe:1"]
    p = subprocess.run(cmd, capture_output=True, creationflags=procutil.CREATE_FLAGS)
    if p.returncode != 0:
        raise ValueError("无法解码：" + p.stderr.decode("utf-8", "replace")[-300:])
    return np.frombuffer(p.stdout, dtype=np.float32).reshape(-1, 2)


def _separate(w44: Path, work: Path) -> tuple[Path, Path]:
    from .engine import engine

    if not engine.installed():
        raise RuntimeError("人声分离需要 GPT-SoVITS 引擎（自带 UVR5），请先安装引擎，或取消“人声分离”。")
    s = config.load_settings()["engine"]
    d = engine.dir
    vocal_dir, other_dir = work / "vocal", work / "other"
    for sub in (vocal_dir, other_dir):
        sub.mkdir(exist_ok=True)
        for f in sub.glob(f"{w44.stem}_*"):
            f.unlink()
    script = Path(__file__).parent / "engine_scripts" / "uvr_separate.py"
    args = [str(d / "runtime" / "python.exe"), "-I", str(script), str(d), UVR_MODEL, str(w44), str(vocal_dir), str(other_dir), s["device"], "1" if s["is_half"] else "0"]
    proc = procutil.spawn(args, cwd=str(d), env=procutil.child_env([str(d / "runtime")]))
    out_lines = []
    assert proc.stdout is not None
    for line in proc.stdout:
        out_lines.append(line.rstrip())
    code = proc.wait()
    done = next((l for l in out_lines if l.startswith("UVR_DONE ")), None)
    if code != 0 or not done:
        tail = "\n".join(out_lines[-6:])
        raise RuntimeError(f"人声分离失败（退出码 {code}）。显存不足时可以先停止声音引擎再试。\n{tail}")
    vocal = Path(done[len("UVR_DONE ") :].strip())
    others = sorted(other_dir.glob(f"{w44.stem}_*"))
    return vocal, (others[0] if others else None)


def _segments(x16: np.ndarray) -> list[tuple[int, int]]:
    """VAD segments merged/limited to sentence-sized clips of 2-10 s (16 kHz sample indices)."""
    raw = [(s, s + len(seg)) for s, seg in asr.segment(x16, min_silence=0.35, min_speech=0.4, max_speech=CLIP_MAX - 0.4)]
    sr = 16000
    merged: list[list[int]] = []
    for a, b in raw:
        if merged:
            pa, pb = merged[-1]
            if a - pb < 0.6 * sr and (b - pa) <= (CLIP_MAX - 0.3) * sr and (pb - pa) < 3.5 * sr:
                merged[-1][1] = b
                continue
        merged.append([a, b])
    pad = int(0.15 * sr)
    out = []
    for a, b in merged:
        a2, b2 = max(0, a - pad), min(len(x16), b + pad)
        if (b2 - a2) / sr >= 1.0:
            out.append((a2, b2))
    return out


def _quality(v16: np.ndarray, orig16: np.ndarray, other16: np.ndarray | None) -> dict[str, Any]:
    clip_ratio = float(np.mean(np.abs(orig16) >= 0.995)) if len(orig16) else 0.0
    frame = 320
    n = len(v16) // frame
    if n >= 4:
        rms = np.sqrt(np.mean(v16[: n * frame].reshape(n, frame) ** 2, axis=1) + 1e-12)
        speech, floor = np.percentile(rms, 90), np.percentile(rms, 10)
        snr = float(np.clip(20 * np.log10(speech / max(floor, 1e-5)), 0, 60))
        peak_db = float(20 * np.log10(max(1e-6, np.max(np.abs(v16)))))
    else:
        snr, peak_db = 0.0, -99.0
    q = {"clipping": round(clip_ratio * 100, 3), "snr_db": round(snr, 1), "peak_db": round(peak_db, 1)}
    if other16 is not None and len(other16):
        ve = float(np.mean(v16**2)) + 1e-12
        oe = float(np.mean(other16**2)) + 1e-12
        q["bgm_db"] = round(10 * np.log10(oe / ve), 1)  # accompaniment energy relative to voice
    return q


def _score(q: dict[str, Any], dur: float, has_text: bool) -> int:
    if not has_text:
        return 0
    if 3 <= dur <= 8:
        d = 1.0
    elif dur < 3:
        d = max(0.25, 0.5 + 0.5 * (dur - 2)) if dur >= 2 else 0.25
    else:
        d = max(0.5, 1 - 0.25 * (dur - 8))
    s = float(np.clip((q["snr_db"] - 12) / 20, 0, 1)) * 0.6 + 0.4
    c = max(0.0, 1 - q["clipping"] * 2)  # 0.5% clipped samples -> score 0
    loud = 1.0 if q["peak_db"] > -30 else 0.6
    return int(round(100 * d * s * c * loud))


# ------------------------------------------------------------------ speaker similarity
def _get_extractor():
    global _extractor
    import sherpa_onnx

    with _lock:
        if _extractor is None:
            m = config.resolve(SPEAKER_MODEL)
            if not m.exists():
                raise RuntimeError("缺少声纹模型（models/speaker），请运行 setup.bat。")
            _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
                sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(m), num_threads=2)
            )
        return _extractor


def _embed(x16: np.ndarray) -> np.ndarray:
    ex = _get_extractor()
    s = ex.create_stream()
    s.accept_waveform(16000, x16.astype(np.float32))
    s.input_finished()
    e = np.array(ex.compute(s), dtype=np.float32)
    return e / (np.linalg.norm(e) + 1e-9)


def _voice_ref_embedding(vid: str) -> np.ndarray | None:
    v = voices.get(vid)
    if not v.get("ref"):
        return None
    return _embed(audio_util.decode(voices.abspath(v, v["ref"]["audio"]), 16000))


def set_target(vid: str, item_ids: list[str] | None = None, use_ref: bool = False) -> dict[str, Any]:
    """Pick the target speaker (clips or the voice's reference) and rescore similarity."""
    with _lock:
        data = load(vid)
        if use_ref:
            t = _voice_ref_embedding(vid)
            if t is None:
                raise ValueError("这个声音还没有参考音频。")
            data["target"] = {"source": "ref", "embedding": t.tolist()}
        else:
            embs = [np.array(i["embedding"], np.float32) for i in data["items"] if i["id"] in set(item_ids or [])]
            if not embs:
                raise ValueError("请选择至少一段确定是目标说话人的片段。")
            t = np.mean(embs, axis=0)
            t /= np.linalg.norm(t) + 1e-9
            data["target"] = {"source": "clips", "items": item_ids, "embedding": t.tolist()}
        for i in data["items"]:
            i["similarity"] = round(float(np.dot(np.array(i["embedding"], np.float32), t)), 3)
        save(vid, data)
    bus.publish("dataset_changed", voice=vid)
    return data


# ------------------------------------------------------------------ review edits
def edit_item(vid: str, item_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        data = load(vid)
        for i in data["items"]:
            if i["id"] == item_id:
                if "text" in patch and patch["text"] is not None:
                    i["text"] = str(patch["text"]).strip()
                    i["score"] = _score(i["quality"], i["duration"], bool(i["text"]))
                if "lang" in patch and patch["lang"] in ("zh", "en", "ja", "ko", "yue"):
                    i["lang"] = patch["lang"]
                if "deleted" in patch and patch["deleted"] is not None:
                    i["deleted"] = bool(patch["deleted"])
                save(vid, data)
                return i
    raise KeyError(item_id)


def bulk(vid: str, ids: list[str] | None = None, action: str = "delete", min_score: int | None = None,
         min_similarity: float | None = None) -> dict[str, Any]:
    """Delete/restore by id list, or delete every kept clip below the thresholds."""
    with _lock:
        data = load(vid)
        n = 0
        idset = set(ids or [])
        for i in data["items"]:
            if action == "filter":
                low = (min_score is not None and i["score"] < min_score) or (
                    min_similarity is not None and i.get("similarity") is not None and i["similarity"] < min_similarity
                )
                if low and not i["deleted"]:
                    i["deleted"] = True
                    n += 1
            elif i["id"] in idset:
                want = action == "delete"
                if i["deleted"] != want:
                    i["deleted"] = want
                    n += 1
        save(vid, data)
    bus.publish("dataset_changed", voice=vid)
    return {"changed": n, "summary": summary(vid)}


def clear(vid: str) -> None:
    import shutil

    with _lock:
        d = ddir(vid)
        for sub in ("clips", "work", "sources"):
            shutil.rmtree(d / sub, ignore_errors=True)
        _items_path(vid).unlink(missing_ok=True)
    bus.publish("dataset_changed", voice=vid)


def export(vid: str) -> dict[str, Any]:
    """Write GPT-SoVITS training list: <wav path>|<speaker>|<LANG>|<text>."""
    data = load(vid)
    clips = ddir(vid) / "clips"
    lines = []
    for i in data["items"]:
        if i.get("deleted") or not i.get("text"):
            continue
        text = i["text"].replace("|", " ").replace("\n", " ").strip()
        lines.append(f"{(clips / (i['id'] + '.wav')).resolve()}|{vid}|{i['lang'].upper()}|{text}")
    if not lines:
        raise ValueError("没有可导出的片段（需要保留且有文字）。")
    out = ddir(vid) / "train.list"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    s = summary(vid, data)
    return {"path": str(out.relative_to(config.ROOT)).replace("\\", "/"), "lines": len(lines), "seconds": s["seconds"], "level": s["level"]}


def public_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [{k: v for k, v in i.items() if k != "embedding"} for i in data["items"]]
