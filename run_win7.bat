@echo off
REM AutoRUN Win7 Launcher — 仅 Web UI 模式
cd /d "%~dp0"
python main.py --web %*
pause
