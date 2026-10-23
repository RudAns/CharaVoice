@echo off
rem Start the CharaVoice window (development mode: Tauri + Python backend from source).
cd /d "%~dp0app"
call pnpm tauri dev
