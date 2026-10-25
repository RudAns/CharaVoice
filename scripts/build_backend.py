"""Freeze the Python backend with PyInstaller (one folder) for the Tauri installer.

    .venv\\Scripts\\python.exe scripts\\build_backend.py

Output: app/src-tauri/resources/backend/charavoice-backend.exe (+ _internal/). The same exe is the
backend (`serve`), the CLI (`speak`, `listen`, ...) and the stdio MCP server (`mcp`).
Models and engines are not included; the app downloads them on first run.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "src-tauri" / "resources"
WORK = ROOT / "build" / "pyinstaller"


def main() -> None:
    entry = WORK / "charavoice_entry.py"
    WORK.mkdir(parents=True, exist_ok=True)
    entry.write_text("import sys\nfrom charavoice.cli import main\nsys.exit(main())\n", encoding="utf-8")
    scripts = ROOT / "backend" / "charavoice" / "engine_scripts"
    args = [
        sys.executable, "-m", "PyInstaller", str(entry),
        "--name", "charavoice-backend", "--onedir", "--console", "--noconfirm", "--clean",
        "--distpath", str(OUT / "_dist"), "--workpath", str(WORK / "work"), "--specpath", str(WORK),
        "--paths", str(ROOT / "backend"),
        "--add-data", f"{scripts}{';'}charavoice/engine_scripts",
        # packages with native libraries or lazy imports PyInstaller cannot see
        "--collect-all", "sherpa_onnx", "--collect-all", "pyworld", "--collect-all", "audioseal",
        "--collect-binaries", "soundfile", "--collect-data", "soundfile", "--collect-binaries", "sounddevice",
        "--collect-all", "pyaudiowpatch", "--collect-submodules", "mcp.server", "--collect-submodules", "mcp.shared",
        "--collect-submodules", "uvicorn",
        "--collect-submodules", "charavoice", "--copy-metadata", "mcp",
        "--hidden-import", "pyloudnorm", "--hidden-import", "scipy.signal",
        "--exclude-module", "tkinter", "--exclude-module", "matplotlib", "--exclude-module", "IPython",
    ]
    print(" ".join(a if " " not in a else f'"{a}"' for a in args), flush=True)
    subprocess.check_call(args, cwd=ROOT)
    dst = OUT / "backend"
    shutil.rmtree(dst, ignore_errors=True)
    (OUT / "_dist" / "charavoice-backend").rename(dst)
    shutil.rmtree(OUT / "_dist", ignore_errors=True)
    size = sum(f.stat().st_size for f in dst.rglob("*") if f.is_file()) / 2**20
    print(f"[build] backend -> {dst} ({size:,.0f} MB)")


if __name__ == "__main__":
    main()
