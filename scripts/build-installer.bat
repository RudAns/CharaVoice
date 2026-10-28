@echo off
rem Build the Windows installer: frozen Python backend + Tauri NSIS bundle.
rem Output: app\src-tauri\target\release\bundle\nsis\CharaVoice_<version>_x64-setup.exe
setlocal
cd /d "%~dp0.."
rem the venv may come from uv (no pip inside), so fall back to `uv pip install`
".venv\Scripts\python.exe" -c "import PyInstaller" >nul 2>&1 || ".venv\Scripts\python.exe" -m pip install pyinstaller || uv pip install --python ".venv\Scripts\python.exe" pyinstaller || exit /b 1
".venv\Scripts\python.exe" scripts\build_backend.py || exit /b 1
cd app
call pnpm tauri build || exit /b 1
echo.
echo Installer: app\src-tauri\target\release\bundle\nsis\
