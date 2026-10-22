@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
title Stop All Voice Services
echo Closing assistant, desktop pet and Ollama...
echo.
"D:\miniconda3\envs\voice-assistant\python.exe" stop.py --all
echo.
pause
