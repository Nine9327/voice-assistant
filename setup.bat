@echo off
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0env.bat"
echo Creating uv environment: %VOICE_VENV%
echo.
uv venv --python 3.11 "%VOICE_VENV%" || goto fail
echo.
echo Installing dependencies...
rem Direct PyPI is very slow in CN for multi-GB CUDA wheels; use the Aliyun mirror
uv pip install -r requirements.txt --python "%VOICE_PY%" --default-index "https://mirrors.aliyun.com/pypi/simple" || goto fail
echo.
echo Done. Now double-click start.vbs (or run.bat) to launch.
pause
exit /b 0

:fail
echo.
echo Setup failed. Check the messages above.
pause
exit /b 1
