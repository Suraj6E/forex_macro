@echo off
rem Replay simulator - always http://127.0.0.1:8001/ (Django stays on :8000).
rem Running this again while it is up just opens the running one.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m simulator %*
