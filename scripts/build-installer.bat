@echo off
rem Build the Windows installer: frozen Python backend + Tauri NSIS bundle.
rem Output: app\src-tauri\target\release\bundle\nsis\CharaVoice_<version>_x64-setup.exe
setlocal
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m pip show pyinstaller >nul 2>&1 || ".venv\Scripts\python.exe" -m pip install pyinstaller || exit /b 1
".venv\Scripts\python.exe" scripts\build_backend.py || exit /b 1
cd app
call pnpm tauri build || exit /b 1
echo.
echo Installer: app\src-tauri\target\release\bundle\nsis\
