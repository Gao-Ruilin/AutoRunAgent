@echo off
call "%~dp0.venv\Scripts\activate.bat" >nul 2>nul
autorun %*
