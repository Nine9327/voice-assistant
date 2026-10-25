@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
title Stop Voice Assistant
call "%~dp0env.bat"
"%VOICE_PY%" stop.py %*
echo.
pause
