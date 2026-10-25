@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
title Stop All Voice Services
echo Closing assistant and desktop pet...
echo Ollama is left running (use stop.py --with-ollama to free VRAM).
echo.
call "%~dp0env.bat"
"%VOICE_PY%" stop.py --all
echo.
pause
