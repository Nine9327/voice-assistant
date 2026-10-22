@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
"D:\miniconda3\envs\voice-assistant\python.exe" assistant.py %*
if errorlevel 1 pause
