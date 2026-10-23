"""One-time setup for CharaVoice (Windows). Standard library only.

    py -3.11 scripts\\setup.py            everything
    py -3.11 scripts\\setup.py --skip-engine

Steps: Python venv + backend package, listening models (Silero VAD, SenseVoice),
GPT-SoVITS official Windows package (~11 GB, resumable), frontend packages.
Set HF_ENDPOINT (for example https://hf-mirror.com) to download through a mirror.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HF = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
ENGINE_PACKAGE = "GPT-SoVITS-v5-20261005.7z"
ENGINE_URL = f"{HF}/lj1995/GPT-SoVITS-windows-package/resolve/main/{ENGINE_PACKAGE}?download=true"
SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
SENSEVOICE = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"


def say(msg: str) -> None:
    print(f"[setup] {msg}", flush=True)


def download(url: str, dst: Path) -> None:
    """Resumable download with a simple progress line."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_suffix(dst.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
    with urllib.request.urlopen(req) as r:
        total = have + int(r.headers.get("Content-Length", 0))
        mode = "ab" if r.status == 206 else "wb"
        done = have if mode == "ab" else 0
        with open(part, mode) as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  {dst.name}: {done / 2**20:,.0f} / {total / 2**20:,.0f} MB", end="", flush=True)
    print()
    part.replace(dst)


def step_venv() -> Path:
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        say("创建 Python 3.11 虚拟环境 .venv")
        if shutil.which("uv"):
            subprocess.check_call(["uv", "venv", str(ROOT / ".venv"), "--python", "3.11"])
        else:
            subprocess.check_call([sys.executable, "-m", "venv", str(ROOT / ".venv")])
    say("安装后端依赖")
    torch_cpu = ["torch", "--index-url", "https://download.pytorch.org/whl/cpu"]
    if shutil.which("uv"):
        subprocess.check_call(["uv", "pip", "install", "--python", str(py), *torch_cpu])
        subprocess.check_call(["uv", "pip", "install", "--python", str(py), "-e", f"{ROOT / 'backend'}[watermark]"])
    else:
        subprocess.check_call([str(py), "-m", "pip", "install", *torch_cpu])
        subprocess.check_call([str(py), "-m", "pip", "install", "-e", f"{ROOT / 'backend'}[watermark]"])
    return py


def step_asr() -> None:
    d = ROOT / "models" / "asr"
    vad = d / "silero_vad.onnx"
    if not vad.exists():
        say("下载 Silero VAD")
        download(f"{SHERPA}/silero_vad.onnx", vad)
    if not (d / SENSEVOICE / "model.int8.onnx").exists():
        say("下载 SenseVoice（约 1GB）")
        archive = d / f"{SENSEVOICE}.tar.bz2"
        download(f"{SHERPA}/{SENSEVOICE}.tar.bz2", archive)
        with tarfile.open(archive) as t:
            t.extractall(d, filter="data")
        archive.unlink()


def find_7z() -> str | None:
    dirs = [os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")]
    candidates = [shutil.which("7z")] + [str(Path(d) / "7-Zip" / "7z.exe") for d in dirs if d]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def step_engine() -> None:
    target = ROOT / "engines" / "GPT-SoVITS"
    if (target / "api_v2.py").exists():
        say("GPT-SoVITS 已安装，跳过")
        return
    seven = find_7z()
    if not seven:
        sys.exit("[setup] 需要 7-Zip 来解压引擎包，请先安装 7-Zip（https://www.7-zip.org/）后重试。")
    archive = ROOT / "engines" / "_downloads" / ENGINE_PACKAGE
    if not archive.exists():
        say(f"下载 GPT-SoVITS 官方整合包 {ENGINE_PACKAGE}（约 11GB，可断点续传）")
        download(ENGINE_URL, archive)
    say("解压引擎包")
    tmp = ROOT / "engines" / "_extract"
    subprocess.check_call([seven, "x", str(archive), f"-o{tmp}", "-y"], stdout=subprocess.DEVNULL)
    inner = next(p for p in tmp.iterdir() if p.is_dir())
    inner.rename(target)
    tmp.rmdir()


def step_speaker_model() -> None:
    p = ROOT / "models" / "speaker" / "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
    if not p.exists():
        say("下载声纹模型（3D-Speaker CAM++）")
        download(f"https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/{p.name}", p)


def step_frontend() -> None:
    pnpm = shutil.which("pnpm")
    if not pnpm:
        say("没有找到 pnpm，跳过界面依赖（只用后端时不需要）。安装 Node.js 后运行：npm i -g pnpm")
        return
    say("安装界面依赖")
    subprocess.check_call([pnpm, "install"], cwd=ROOT / "app")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-engine", action="store_true")
    ap.add_argument("--skip-frontend", action="store_true")
    a = ap.parse_args()
    step_venv()
    step_asr()
    step_speaker_model()
    if not a.skip_engine:
        step_engine()
    if not a.skip_frontend:
        step_frontend()
    say("完成。运行 start.bat 启动 CharaVoice。")


if __name__ == "__main__":
    main()
