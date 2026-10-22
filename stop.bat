@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
title Stop Voice Assistant
"D:\miniconda3\envs\voice-assistant\python.exe" stop.py %*
echo.
pause
