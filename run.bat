@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
call "%~dp0env.bat"
if not exist "%VOICE_PY%" (
    echo Interpreter not found: %VOICE_PY%
    echo Double-click setup.bat to create it.
    pause
    exit /b 1
)
"%VOICE_PY%" "%~dp0assistant.py" %*
if errorlevel 1 pause
