"""Fine-tuning a voice with the GPT-SoVITS package, driven the same way its webui.py does.

Stages (all run with the package's own Python as child processes):
    1a  prepare_datasets/1-get-text.py          text -> phonemes + BERT features
    1b  prepare_datasets/2-get-hubert-wav32k.py  HuBERT features + 32 kHz wavs
        prepare_datasets/2-get-sv.py             speaker embeddings (v2Pro family only)
    1c  prepare_datasets/3-get-semantic.py       semantic tokens
    s2  GPT_SoVITS/s2_train.py --config <json>   SoVITS fine-tune (template configs/s2<ver>.json)
    s1  GPT_SoVITS/s1_train.py --config_file <yaml>  GPT fine-tune (template configs/s1longer-v2.yaml)
    test  synthesize fixed sentences with the new weights

Everything is written under data/voices/<id>/train (experiment) and .../weights. Both training
scripts resume on their own from the experiment folder (s2: latest G_/D_ checkpoint in
logs_s2_<ver>; s1: newest Lightning ckpt), so "continue" simply re-runs with the same folder;
finished preparation stages are skipped when their outputs exist.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config, dataset, procutil, voices
from .events import bus

log = logging.getLogger("charavoice.training")

TEST_SENTENCES = {
    "zh": "你好，这是训练完成后的试听句子，听听像不像。",
    "en": "Hello, this is a test sentence after training. How does it sound?",
    "ja": "こんにちは、これは学習後のテスト音声です。",
}
STAGES = [
    ("check", "开训前检查"),
    ("1a", "整理文本与 BERT 特征"),
    ("1b", "提取声学与声纹特征"),
    ("1c", "提取语义 token"),
    ("s2", "训练 SoVITS（音色）"),
    ("s1", "训练 GPT（语调与节奏）"),
    ("test", "生成试听"),
]


def tdir(vid: str) -> Path:
    return voices.folder(vid) / "train"


def exp_dir(vid: str) -> Path:
    return tdir(vid) / "exp"


# ------------------------------------------------------------------ pre-flight
def gpu_info() -> dict[str, Any]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, creationflags=procutil.CREATE_FLAGS,
        ).stdout.strip().splitlines()
        name, total, used, free = [x.strip() for x in out[0].split(",")]
        return {"ok": True, "name": name, "total_mb": int(total), "used_mb": int(used), "free_mb": int(free)}
    except Exception as e:
        return {"ok": False, "error": f"没有检测到 NVIDIA 显卡（{e}）"}


def recommend(free_mb: int, total_seconds: float) -> dict[str, Any]:
    """Batch sizes from free VRAM (webui uses total GB // 2; we budget on what is actually free)."""
    gb = free_mb / 1024
    s2_bs = int(max(1, min(8, gb // 2)))
    s1_bs = int(max(1, min(8, gb // 2)))
    s2_epochs = 8 if total_seconds >= 300 else 10
    s1_epochs = 15
    return {"s2_batch": s2_bs, "s1_batch": s1_bs, "s2_epochs": s2_epochs, "s1_epochs": s1_epochs}


def precheck(vid: str) -> dict[str, Any]:
    from .engine import engine

    v = voices.get(vid)
    summ = dataset.summary(vid)
    g = gpu_info()
    disk = shutil.disk_usage(config.DATA_DIR)
    items: list[dict[str, Any]] = []

    def add(key: str, ok: bool, label: str, detail: str, blocking: bool = True):
        items.append({"key": key, "ok": ok, "label": label, "detail": detail, "blocking": blocking and not ok})

    add("engine", engine.installed(), "GPT-SoVITS 引擎", "已安装" if engine.installed() else "没有找到引擎，请运行 setup.bat")
    add("base", (v.get("base") or "v2Pro") in ("v2Pro", "v2ProPlus"), "基础模型",
        f"{v.get('base')}（训练支持 v2Pro / v2ProPlus）")
    if g["ok"]:
        # the inference engine is stopped during training, so its share counts as available
        engine_mb = 3500 if engine.state == "ready" else 0
        avail = g["free_mb"] + engine_mb
        add("gpu", True, "显卡", f"{g['name']}，共 {g['total_mb'] / 1024:.0f}GB", False)
        add("vram", avail >= 4000, "可用显存",
            f"约 {avail / 1024:.1f}GB 可用（训练前会暂停推理引擎）" + ("" if avail >= 4000 else "；至少需要 4GB，请关闭占用显卡的程序（游戏、模拟器、Unity 等）"))
        if 4000 <= avail < 6000:
            items[-1]["detail"] += "；显存偏少，会自动用小批量训练，速度较慢"
    else:
        avail = 0
        add("gpu", False, "显卡", g["error"])
    add("disk", disk.free > 6 * 1024**3, "磁盘空间", f"剩余 {disk.free / 1024**3:.0f}GB（需要约 6GB）")
    secs = summ["seconds"]
    add("data", secs >= dataset.MIN_TOTAL, "素材时长",
        f"{secs / 60:.1f} 分钟" + ("" if secs >= dataset.MIN_TOTAL else "，至少需要 1 分钟") +
        ("" if secs >= dataset.RECOMMENDED_TOTAL or secs < dataset.MIN_TOTAL else "（建议 10 分钟以上）"))
    rec = recommend(avail, secs) if avail else None
    state = load_state(vid)
    return {
        "ok": not any(i["blocking"] for i in items),
        "items": items,
        "recommend": rec,
        "resumable": bool(state.get("stage")) and state.get("status") in ("cancelled", "error", "running"),
        "state": state,
    }


# ------------------------------------------------------------------ state
def load_state(vid: str) -> dict[str, Any]:
    p = tdir(vid) / "state.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(vid: str, st: dict[str, Any]) -> None:
    tdir(vid).mkdir(parents=True, exist_ok=True)
    (tdir(vid) / "state.json").write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


@dataclass
class TrainJob:
    voice: str
    params: dict[str, Any]
    id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S"))
    status: str = "running"  # running | done | error | cancelled
    stage: str = "check"
    stage_label: str = "开训前检查"
    progress: float = 0.0  # 0..1 overall
    stage_progress: float = 0.0
    detail: str = ""
    message: str = ""
    started: float = field(default_factory=time.time)
    samples: dict[str, str] = field(default_factory=dict)
    cancel: bool = False
    proc: subprocess.Popen | None = None

    def public(self) -> dict[str, Any]:
        # not dataclasses.asdict: it deep-copies every field, including the Popen handle
        d = {k: getattr(self, k) for k in self.__dataclass_fields__ if k not in ("cancel", "proc")}
        d["params"] = dict(self.params)
        d["samples"] = dict(self.samples)
        d["elapsed"] = round(time.time() - self.started)
        return d


jobs: dict[str, TrainJob] = {}
logs: dict[str, list[str]] = {}
_lock = threading.Lock()
_WEIGHTS = {  # stage -> share of the whole run, for the overall progress bar
    "check": 0.01, "1a": 0.04, "1b": 0.08, "1c": 0.04, "s2": 0.45, "s1": 0.33, "test": 0.05,
}


class Cancelled(Exception):
    pass


def _emit(job: TrainJob) -> None:
    keys = [k for k, _ in STAGES]
    done = sum(_WEIGHTS[k] for k in keys[: keys.index(job.stage)])
    job.progress = round(min(1.0, done + _WEIGHTS[job.stage] * job.stage_progress), 4)
    bus.publish("train_job", **job.public())
    st = load_state(job.voice)
    st.update(status=job.status, stage=job.stage, id=job.id, params=job.params, updated=voices.now_iso(), message=job.message)
    save_state(job.voice, st)


def _log(vid: str, line: str) -> None:
    buf = logs.setdefault(vid, [])
    buf.append(line)
    if len(buf) > 2000:
        del buf[:500]
    bus.publish("train_log", voice=vid, line=line[-400:])


def start(vid: str, params: dict[str, Any]) -> TrainJob:
    with _lock:
        j = jobs.get(vid)
        if j and j.status == "running":
            raise ValueError("这个声音已经在训练了。")
        if any(x.status == "running" for x in jobs.values()):
            raise ValueError("另一个声音正在训练，一次只能训练一个。")
        chk = precheck(vid)
        if not chk["ok"]:
            bad = "；".join(f"{i['label']}：{i['detail']}" for i in chk["items"] if i["blocking"])
            raise ValueError(f"开训前检查没有通过：{bad}")
        rec = chk["recommend"] or {}
        p = {**rec, **{k: v for k, v in params.items() if v is not None}}
        p["s2_batch"] = int(max(1, min(32, p.get("s2_batch", 2))))
        p["s1_batch"] = int(max(1, min(32, p.get("s1_batch", 2))))
        p["s2_epochs"] = int(max(1, min(25, p.get("s2_epochs", 8))))
        p["s1_epochs"] = int(max(1, min(50, p.get("s1_epochs", 15))))
        if p.get("fresh"):
            shutil.rmtree(tdir(vid), ignore_errors=True)
        job = TrainJob(voice=vid, params=p)
        jobs[vid] = job
        logs[vid] = []
    threading.Thread(target=_run, args=(job,), daemon=True, name=f"train-{vid}").start()
    return job


def cancel(vid: str) -> bool:
    j = jobs.get(vid)
    if not j or j.status != "running":
        return False
    j.cancel = True
    _kill(j)
    return True


def _kill(job: TrainJob) -> None:
    p = job.proc
    if p and p.poll() is None:
        # the trainers spawn DataLoader workers; take the whole tree down
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, creationflags=procutil.CREATE_FLAGS)


# ------------------------------------------------------------------ runner
def _run(job: TrainJob) -> None:
    import asyncio

    from .engine import engine

    vid = job.voice
    loop = bus._loop
    restart_engine = engine.state in ("ready", "starting")

    def on_loop(coro):
        assert loop is not None
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=180)

    try:
        v = voices.get(vid)
        version = v.get("base") or "v2Pro"
        _stage(job, "check")
        exp = exp_dir(vid)
        exp.mkdir(parents=True, exist_ok=True)
        ex = dataset.export(vid)
        _log(vid, f"训练列表：{ex['lines']} 段，{ex['seconds']:.0f} 秒")
        engine.hold = "正在训练声音，训练期间暂停语音合成（训练结束会自动恢复）。"
        if engine.state != "stopped":
            _log(vid, "暂停推理引擎以释放显存")
            on_loop(engine.stop())
        _prepare(job, version, exp)
        sovits = _train_s2(job, version, exp)
        gpt = _train_s1(job, version, exp)
        rel = lambda p: str(Path(p).resolve().relative_to(voices.folder(vid).resolve())).replace("\\", "/")
        voices.set_weights(vid, rel(gpt), rel(sovits))
        _stage(job, "test")
        engine.hold = None
        on_loop(engine.start())
        restart_engine = False
        job.samples = on_loop(_samples(vid))
        job.status = "done"
        job.message = "训练完成，已切换到新权重。"
        _emit(job)
    except Cancelled:
        job.status = "cancelled"
        job.message = "已中断。再次开始训练会从上次保存的进度继续。"
        _emit(job)
    except Exception as e:
        log.exception("training failed")
        job.status = "error"
        job.message = str(e)
        _emit(job)
    finally:
        engine.hold = None
        if restart_engine:
            try:
                on_loop(engine.start())
            except Exception:
                log.exception("engine restart after training failed")


def _stage(job: TrainJob, key: str, detail: str = "") -> None:
    if job.cancel:
        raise Cancelled()
    job.stage = key
    job.stage_label = dict(STAGES)[key]
    job.stage_progress = 0.0
    job.detail = detail
    _log(job.voice, f"== {job.stage_label} ==")
    _emit(job)


def _engine_dir() -> Path:
    from .engine import engine

    return engine.dir


def _exec(job: TrainJob, args: list[str], env_extra: dict[str, str], parse=None) -> None:
    """Run one package script to completion, streaming its log; raise on failure/cancel."""
    d = _engine_dir()
    env = procutil.child_env([str(d / "runtime")])
    env.update({k: str(v) for k, v in env_extra.items()})
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [str(d / "runtime" / "python.exe"), "-s", *args]
    _log(job.voice, "$ " + " ".join(Path(a).name if os.sep in a or "/" in a else a for a in cmd[1:]))
    job.proc = procutil.spawn(cmd, cwd=str(d), env=env, encoding="utf-8")
    tail: list[str] = []
    assert job.proc.stdout is not None
    for line in job.proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        for part in line.split("\r"):
            part = part.strip()
            if part:
                tail = (tail + [part])[-30:]
                if parse:
                    parse(part)
                if not re.match(r"^\s*\d+%\|", part):  # skip tqdm bars in the log view
                    _log(job.voice, part)
        if job.cancel:
            break
    code = job.proc.wait()
    job.proc = None
    if job.cancel:
        raise Cancelled()
    if code != 0:
        hint = ""
        joined = "\n".join(tail)
        if "out of memory" in joined.lower() or "CUDA error" in joined:
            hint = "（显存不足：请关闭占用显卡的程序，或把批量大小调小后继续）"
        raise RuntimeError(f"{Path(args[0]).name} 失败，退出码 {code}{hint}\n" + "\n".join(tail[-8:]))


def _prepare(job: TrainJob, version: str, exp: Path) -> None:
    vid = job.voice
    d = _engine_dir()
    inp_text = str((dataset.ddir(vid) / "train.list").resolve())
    common = {
        "inp_text": inp_text,
        "inp_wav_dir": str((dataset.ddir(vid) / "clips").resolve()),
        "exp_name": vid,
        "opt_dir": str(exp.resolve()),
        "i_part": "0",
        "all_parts": "1",
        "_CUDA_VISIBLE_DEVICES": "0",
        "is_half": "True" if config.load_settings()["engine"]["is_half"] else "False",
        "version": version,
    }
    pm = d / "GPT_SoVITS" / "pretrained_models"
    # 1a
    _stage(job, "1a")
    if not _nonempty(exp / "2-name2text.txt", 2):
        _exec(job, ["GPT_SoVITS/prepare_datasets/1-get-text.py"], {**common, "bert_pretrained_dir": str(pm / "chinese-roberta-wwm-ext-large")})
        part = exp / "2-name2text-0.txt"
        if not part.exists() or not part.read_text(encoding="utf-8").strip():
            raise RuntimeError("文本处理没有产出（检查素材文字是否为空）")
        part.replace(exp / "2-name2text.txt")
    # 1b
    _stage(job, "1b")
    if not (exp / "5-wav32k").exists() or not any((exp / "5-wav32k").iterdir()):
        _exec(job, ["GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py"], {**common, "cnhubert_base_dir": str(pm / "chinese-hubert-base")})
    job.stage_progress = 0.6
    _emit(job)
    if "Pro" in version and (not (exp / "7-sv_cn").exists() or not any((exp / "7-sv_cn").iterdir())):
        _exec(job, ["GPT_SoVITS/prepare_datasets/2-get-sv.py"], {**common, "sv_path": str(pm / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt")})
    # 1c
    _stage(job, "1c")
    sem = exp / "6-name2semantic.tsv"
    if not sem.exists() or sem.stat().st_size < 31:
        from .engine import BASE_MODELS

        _exec(job, ["GPT_SoVITS/prepare_datasets/3-get-semantic.py"], {
            **common,
            "pretrained_s2G": str((d / BASE_MODELS[version][1]).resolve()),
            "s2config_path": f"GPT_SoVITS/configs/s2{version}.json",
        })
        part = exp / "6-name2semantic-0.tsv"
        lines = part.read_text(encoding="utf-8").strip("\n").split("\n") if part.exists() else []
        if not lines:
            raise RuntimeError("语义 token 提取没有产出")
        sem.write_text("\n".join(["item_name\tsemantic_audio"] + lines) + "\n", encoding="utf-8")
        part.unlink()


def _nonempty(p: Path, min_lines: int) -> bool:
    return p.exists() and len(p.read_text(encoding="utf-8").strip("\n").split("\n")) >= min_lines


def _train_s2(job: TrainJob, version: str, exp: Path) -> Path:
    from .engine import BASE_MODELS

    _stage(job, "s2")
    d = _engine_dir()
    vid = job.voice
    p = job.params
    out = voices.folder(vid) / "weights" / "sovits"
    out.mkdir(parents=True, exist_ok=True)
    data = json.loads((d / "GPT_SoVITS" / "configs" / f"s2{version}.json").read_text(encoding="utf-8"))
    is_half = config.load_settings()["engine"]["is_half"]
    g = (d / BASE_MODELS[version][1]).resolve()
    data["train"].update({
        "batch_size": p["s2_batch"], "epochs": p["s2_epochs"], "text_low_lr_rate": 0.4,
        "pretrained_s2G": str(g), "pretrained_s2D": str(g).replace("s2G", "s2D"),
        "if_save_latest": True, "if_save_every_weights": True, "save_every_epoch": 1 if p["s2_epochs"] <= 10 else 2,
        "gpu_numbers": "0", "grad_ckpt": False, "lora_rank": 32, "fp16_run": bool(is_half),
    })
    data["model"]["version"] = version
    data["data"]["exp_dir"] = data["s2_ckpt_dir"] = str(exp.resolve())
    data["save_weight_dir"] = str(out.resolve())
    data["name"] = vid
    data["version"] = version
    (exp / f"logs_s2_{version}").mkdir(parents=True, exist_ok=True)  # webui creates it before launching
    cfg = tdir(vid) / "s2.json"
    cfg.write_text(json.dumps(data), encoding="utf-8")
    total = p["s2_epochs"]

    def parse(line: str):
        m = re.search(r"Epoch: (\d+)", line) or re.search(r"Train Epoch: (\d+)", line)
        if m:
            done = int(m.group(1)) if "====>" in line else int(m.group(1)) - 1
            job.stage_progress = min(1.0, done / total)
            job.detail = f"第 {min(total, done + (0 if '====>' in line else 1))} / {total} 轮"
            _emit(job)

    _exec(job, ["GPT_SoVITS/s2_train.py", "--config", str(cfg)], {"version": version, "_CUDA_VISIBLE_DEVICES": "0"}, parse)
    files = sorted(out.glob(f"{vid}_e*_s*.pth"), key=lambda f: f.stat().st_mtime)
    if not files:
        raise RuntimeError("SoVITS 训练结束但没有导出权重")
    for f in files[:-1]:
        f.unlink(missing_ok=True)
    return files[-1]


def _train_s1(job: TrainJob, version: str, exp: Path) -> Path:
    import yaml  # bundled with the backend venv via uvicorn[standard]

    _stage(job, "s1")
    d = _engine_dir()
    vid = job.voice
    p = job.params
    out = voices.folder(vid) / "weights" / "gpt"
    out.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_load((d / "GPT_SoVITS" / "configs" / "s1longer-v2.yaml").read_text(encoding="utf-8"))
    from .engine import BASE_MODELS

    is_half = config.load_settings()["engine"]["is_half"]
    data["train"].update({
        "batch_size": p["s1_batch"], "epochs": p["s1_epochs"], "save_every_n_epoch": 3 if p["s1_epochs"] > 6 else 1,
        "if_save_every_weights": True, "if_save_latest": True, "if_dpo": False,
        "half_weights_save_dir": str(out.resolve()), "exp_name": vid,
    })
    if not is_half:
        data["train"]["precision"] = "32"
    data["data"]["num_workers"] = 2
    data["pretrained_s1"] = str((d / BASE_MODELS[version][0]).resolve())
    data["train_semantic_path"] = str((exp / "6-name2semantic.tsv").resolve())
    data["train_phoneme_path"] = str((exp / "2-name2text.txt").resolve())
    data["output_dir"] = str((exp / f"logs_s1_{version}").resolve())
    (exp / "logs_s1").mkdir(parents=True, exist_ok=True)
    cfg = tdir(vid) / "s1.yaml"
    cfg.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True), encoding="utf-8")
    total = p["s1_epochs"]

    def parse(line: str):
        m = re.search(r"Epoch (\d+):\s+(\d+)%", line)
        if m:
            ep, pct = int(m.group(1)), int(m.group(2))
            job.stage_progress = min(1.0, (ep + pct / 100) / total)
            job.detail = f"第 {ep + 1} / {total} 轮"
            _emit(job)

    _exec(job, ["GPT_SoVITS/s1_train.py", "--config_file", str(cfg)], {"_CUDA_VISIBLE_DEVICES": "0", "hz": "25hz", "version": version}, parse)
    files = sorted(out.glob(f"{vid}-e*.ckpt"), key=lambda f: f.stat().st_mtime)
    if not files:
        raise RuntimeError("GPT 训练结束但没有导出权重")
    for f in files[:-1]:
        f.unlink(missing_ok=True)
    return files[-1]


async def _samples(vid: str) -> dict[str, str]:
    """Synthesize the fixed test sentences with the new weights (files under train/samples)."""
    import soundfile as sf

    from . import speech

    out = tdir(vid) / "samples"
    out.mkdir(parents=True, exist_ok=True)
    v = voices.get(vid)
    res = {}
    for lang, text in TEST_SENTENCES.items():
        req = speech.build_request(v, text, lang, streaming_mode=0)
        from .engine import engine

        chunks = []
        sr = 32000
        async for sr, pcm in engine.synthesize(req, presence=False):
            chunks.append(pcm)
        import numpy as np

        sf.write(str(out / f"{lang}.wav"), np.concatenate(chunks), sr, subtype="PCM_16")
        res[lang] = f"{lang}.wav"
    return res


def sample_path(vid: str, name: str) -> Path | None:
    p = (tdir(vid) / "samples" / name)
    return p if p.exists() and p.parent.resolve() == (tdir(vid) / "samples").resolve() else None
