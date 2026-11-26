@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Python environment is missing. See scripts\README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "app\server.py" --open
if errorlevel 1 pause
