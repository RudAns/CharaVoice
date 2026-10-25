"""Model manager: engines and models are downloaded on first run, never shipped with the app.

Each asset has a purpose, licence, size and a list of steps (download with resume, extract,
install). Jobs run one at a time in a thread and report progress on the event bus
({"type": "asset", id, state, progress, message}); the UI shows them on the Models page.
Set download.hf_endpoint (for example https://hf-mirror.com) to fetch Hugging Face files through a mirror.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from . import config
from .events import bus
from .procutil import CREATE_FLAGS

SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SENSEVOICE_DIR = "models/asr/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
SENSEVOICE_HF = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
SPEAKER = "models/speaker/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
ENGINE_PACKAGE = "GPT-SoVITS-v5-20261005.7z"
SEVEN_ZR = "https://www.7-zip.org/a/7zr.exe"
UV_ZIP = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"


def hf(repo: str, path: str) -> str:
    base = (config.load_settings().get("download") or {}).get("hf_endpoint") or os.environ.get("HF_ENDPOINT") or "https://huggingface.co"
    return f"{base.rstrip('/')}/{repo}/resolve/main/{path}?download=true"


@dataclass
class Asset:
    id: str
    name: str
    purpose: str
    license: str
    license_url: str
    size_mb: int
    required: bool
    installed: Callable[[], bool]
    install: Callable[["Job"], None]
    remove_paths: list[str] = field(default_factory=list)
    note: str = ""

    def info(self) -> dict[str, Any]:
        job = JOBS.get(self.id)
        return {
            "id": self.id, "name": self.name, "purpose": self.purpose, "license": self.license, "license_url": self.license_url,
            "size_mb": self.size_mb, "required": self.required, "installed": self.installed(), "note": self.note,
            "job": job.state() if job else None,
        }


class Cancelled(Exception):
    pass


@dataclass
class Job:
    asset: str
    progress: float = 0.0
    message: str = ""
    status: str = "running"  # running | done | error | cancelled
    cancel: threading.Event = field(default_factory=threading.Event)
    started: float = field(default_factory=time.time)

    def state(self) -> dict[str, Any]:
        return {"status": self.status, "progress": round(self.progress, 4), "message": self.message}

    def report(self, progress: float | None = None, message: str | None = None) -> None:
        if self.cancel.is_set():
            raise Cancelled()
        if progress is not None:
            self.progress = progress
        if message is not None:
            self.message = message
        bus.publish("asset", id=self.asset, **self.state())


JOBS: dict[str, Job] = {}
_lock = threading.Lock()


def _p(rel: str) -> Path:
    return config.resolve(rel)


def download(job: Job, url: str, dst: Path, lo: float, hi: float, label: str) -> None:
    """Resumable download into dst (via dst.part), mapping its progress onto [lo, hi]."""
    if dst.exists():
        job.report(hi)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=httpx.Timeout(30, read=120)) as r:
        if r.status_code == 416:  # already complete
            part.replace(dst)
            return
        r.raise_for_status()
        resumed = r.status_code == 206
        total = int(r.headers.get("content-length", 0)) + (have if resumed else 0)
        done = have if resumed else 0
        last = 0.0
        t0, d0 = time.time(), done
        with open(part, "ab" if resumed else "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last > 0.5:
                    last = now
                    speed = (done - d0) / max(now - t0, 1e-3) / 2**20
                    frac = done / total if total else 0
                    job.report(lo + (hi - lo) * frac, f"{label}：{done / 2**20:,.0f} / {total / 2**20:,.0f} MB（{speed:.1f} MB/s）")
    part.replace(dst)
    job.report(hi)


# ------------------------------------------------------------------ installers
def _install_vad(job: Job) -> None:
    download(job, f"{SHERPA}/asr-models/silero_vad.onnx", _p("models/asr/silero_vad.onnx"), 0, 1, "Silero VAD")


def _install_sensevoice(job: Job) -> None:
    d = _p(SENSEVOICE_DIR)
    download(job, hf(SENSEVOICE_HF, "tokens.txt"), d / "tokens.txt", 0, 0.01, "词表")
    download(job, hf(SENSEVOICE_HF, "model.int8.onnx"), d / "model.int8.onnx", 0.01, 1, "SenseVoice")


def _install_speaker(job: Job) -> None:
    download(job, f"{SHERPA}/speaker-recongition-models/{Path(SPEAKER).name}", _p(SPEAKER), 0, 1, "CAM++")


def _engine_dir() -> Path:
    return config.resolve(config.load_settings()["engine"]["dir"])


def _install_gpt_sovits(job: Job) -> None:
    target = _engine_dir()
    if (target / "api_v2.py").exists():
        return
    tools = _p("engines/_tools")
    seven = tools / "7zr.exe"
    download(job, SEVEN_ZR, seven, 0, 0.002, "7-Zip 解压工具")
    archive = _p(f"engines/_downloads/{ENGINE_PACKAGE}")
    download(job, hf("lj1995/GPT-SoVITS-windows-package", ENGINE_PACKAGE), archive, 0.002, 0.85, "GPT-SoVITS 整合包")
    tmp = _p("engines/_extract")
    shutil.rmtree(tmp, ignore_errors=True)
    job.report(0.85, "解压引擎包（约 10 分钟）")
    proc = subprocess.Popen([str(seven), "x", str(archive), f"-o{tmp}", "-y", "-bsp1"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=CREATE_FLAGS)
    buf = b""
    assert proc.stdout is not None
    while True:
        b = proc.stdout.read(64)
        if not b:
            break
        buf = (buf + b)[-200:]
        if job.cancel.is_set():
            proc.kill()
            raise Cancelled()
        txt = buf.decode("ascii", "ignore")
        pct = [int(t[:-1]) for t in txt.replace("\b", " ").split() if t.endswith("%") and t[:-1].isdigit()]
        if pct:
            job.report(0.85 + 0.14 * pct[-1] / 100, f"解压引擎包：{pct[-1]}%")
    if proc.wait() != 0:
        raise RuntimeError("解压失败，压缩包可能不完整，请删除后重新下载。")
    inner = next(p for p in tmp.iterdir() if p.is_dir())
    target.parent.mkdir(parents=True, exist_ok=True)
    inner.rename(target)
    shutil.rmtree(tmp, ignore_errors=True)
    archive.unlink(missing_ok=True)


def _uv() -> Path:
    found = shutil.which("uv")
    return Path(found) if found else _p("engines/_tools/uv.exe")


def _install_omnivoice(job: Job) -> None:
    uv = _uv()
    if not uv.exists():
        z = _p("engines/_tools/uv.zip")
        download(job, UV_ZIP, z, 0, 0.03, "uv")
        with zipfile.ZipFile(z) as zf:
            for n in zf.namelist():
                if n.endswith("uv.exe"):
                    uv.write_bytes(zf.read(n))
        z.unlink(missing_ok=True)
    d = _p("engines/omnivoice")
    py = d / ".venv" / "Scripts" / "python.exe"

    def run(args: list[str], lo: float, hi: float, label: str) -> None:
        job.report(lo, label)
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=CREATE_FLAGS)
        assert p.stdout is not None
        for line in iter(p.stdout.readline, b""):
            if job.cancel.is_set():
                p.kill()
                raise Cancelled()
            t = line.decode("utf-8", "replace").strip()
            if t:
                job.report(None, f"{label}：{t[-90:]}")
        if p.wait() != 0:
            raise RuntimeError(f"{label}失败，请查看网络后重试。")
        job.report(hi)

    if not py.exists():
        d.mkdir(parents=True, exist_ok=True)
        run([str(uv), "venv", str(d / ".venv"), "--python", "3.11"], 0.03, 0.08, "创建 Python 环境")
    run([str(uv), "pip", "install", "--python", str(py), "torch==2.8.0+cu128", "torchaudio==2.8.0+cu128",
         "--extra-index-url", "https://download.pytorch.org/whl/cu128"], 0.08, 0.7, "安装 CUDA 版 PyTorch")
    run([str(uv), "pip", "install", "--python", str(py), "omnivoice", "fastapi", "uvicorn[standard]", "soundfile"], 0.7, 1.0, "安装 OmniVoice")


def _gpt_sovits_installed() -> bool:
    d = _engine_dir()
    return (d / "api_v2.py").exists() and (d / "runtime" / "python.exe").exists()


def _omni_installed() -> bool:
    d = _p("engines/omnivoice")
    return (d / ".venv" / "Scripts" / "python.exe").exists() and (d / ".venv" / "Lib" / "site-packages" / "omnivoice").exists()


ASSETS: dict[str, Asset] = {a.id: a for a in [
    Asset("vad", "Silero VAD", "判断什么时候在说话（听写必需）", "MIT", "https://github.com/snakers4/silero-vad", 1, True,
          lambda: _p("models/asr/silero_vad.onnx").exists(), _install_vad, ["models/asr/silero_vad.onnx"]),
    Asset("sensevoice", "SenseVoice-Small（int8）", "中英日韩粤语音识别：听写、素材转写、参考文本识别（必需）",
          "FunASR Model License 1.1（需保留来源与模型名）", "https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE", 240, True,
          lambda: (_p(SENSEVOICE_DIR) / "model.int8.onnx").exists() and (_p(SENSEVOICE_DIR) / "tokens.txt").exists(),
          _install_sensevoice, [SENSEVOICE_DIR]),
    Asset("speaker", "3D-Speaker CAM++", "声纹相似度：素材筛选、声音相似度评估", "Apache-2.0", "https://github.com/modelscope/3D-Speaker", 28, True,
          lambda: _p(SPEAKER).exists(), _install_speaker, [SPEAKER]),
    Asset("gpt-sovits", "GPT-SoVITS 官方整合包", "语音合成与训练引擎（说话必需，需 NVIDIA 显卡）", "MIT", "https://github.com/RVC-Boss/GPT-SoVITS",
          10870, True, _gpt_sovits_installed, _install_gpt_sovits, ["engines/GPT-SoVITS"],
          "下载约 10.9 GB、解压后约 15 GB，可断点续传。"),
    Asset("omnivoice", "OmniVoice 零样本引擎（可选）", "不训练也能高相似的零样本声音、用文字设计声音", "代码 Apache-2.0；权重以模型页为准",
          "https://github.com/k2-fsa/OmniVoice", 5000, False, _omni_installed, _install_omnivoice, ["engines/omnivoice"],
          "安装约 5 GB（CUDA 版 PyTorch），首次启动时再下载约 3 GB 模型权重。"),
]}


def listing() -> list[dict[str, Any]]:
    return [a.info() for a in ASSETS.values()]


def missing_required() -> list[str]:
    return [a.id for a in ASSETS.values() if a.required and not a.installed()]


def start(aid: str) -> Job:
    a = ASSETS.get(aid)
    if not a:
        raise LookupError(f"没有这个组件：{aid}")
    with _lock:
        running = [j for j in JOBS.values() if j.status == "running"]
        if running:
            if running[0].asset == aid:
                return running[0]
            raise ValueError(f"正在安装 {ASSETS[running[0].asset].name}，请等它完成后再装下一个。")
        job = JOBS[aid] = Job(aid)

    def work() -> None:
        try:
            job.report(0, "开始")
            a.install(job)
            job.status, job.progress, job.message = "done", 1.0, "已安装"
        except Cancelled:
            job.status, job.message = "cancelled", "已取消（已下载的部分会保留，下次继续）"
        except Exception as e:
            job.status, job.message = "error", f"安装失败：{e}"
        bus.publish("asset", id=aid, **job.state())

    threading.Thread(target=work, daemon=True, name=f"asset-{aid}").start()
    return job


def cancel(aid: str) -> bool:
    job = JOBS.get(aid)
    if job and job.status == "running":
        job.cancel.set()
        return True
    return False


def remove(aid: str) -> None:
    a = ASSETS.get(aid)
    if not a:
        raise LookupError(f"没有这个组件：{aid}")
    if JOBS.get(aid) and JOBS[aid].status == "running":
        raise ValueError("正在安装，先取消再删除。")
    root = config.ROOT.resolve()
    for rel in a.remove_paths:
        p = _engine_dir() if aid == "gpt-sovits" else _p(rel)
        if root not in p.resolve().parents:  # never delete something the user pointed us at elsewhere
            raise ValueError(f"{p} 不在 CharaVoice 的数据目录里（是在设置里指定的自定义位置），请手动删除。")
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
    JOBS.pop(aid, None)
    bus.publish("asset", id=aid, status="removed", progress=0, message="已删除")
