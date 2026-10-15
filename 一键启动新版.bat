@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Python environment missing. See docs\VNext-Runbook.md.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" "start_next.py" %*
if errorlevel 1 pause
