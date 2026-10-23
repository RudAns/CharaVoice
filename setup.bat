@echo off
rem One-time setup: Python env, models, GPT-SoVITS engine, UI packages.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3.11 scripts\setup.py %*
) else (
  python scripts\setup.py %*
)
pause
