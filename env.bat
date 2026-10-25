@echo off
rem ============================================================
rem Single place to change the interpreter path.
rem The venv lives inside the project (.venv\). It used to be kept
rem outside on purpose: this folder's ACL had DENY entries that
rem made any .exe created under it (uv venv trampolines) fail with
rem "permission denied". Compensating allow ACEs were added later;
rem re-verified working on 2026-10-25 before moving the venv back.
rem ============================================================
set "VOICE_VENV=%~dp0.venv"
set "VOICE_PY=%VOICE_VENV%\Scripts\python.exe"
set "VOICE_PYW=%VOICE_VENV%\Scripts\pythonw.exe"
