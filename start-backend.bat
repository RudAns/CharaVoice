@echo off
rem Start only the backend (HTTP / MCP for agents, no window).
cd /d "%~dp0"
".venv\Scripts\python.exe" -m charavoice serve
