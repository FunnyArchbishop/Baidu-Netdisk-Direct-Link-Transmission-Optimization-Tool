@echo off
setlocal
cd /d "%~dp0"
set "NETDISK_PYTHON=%~dp0.venv-web\Scripts\python.exe"
if not exist "%NETDISK_PYTHON%" (
  echo Missing .venv-web. See README.md for setup instructions.
  pause
  exit /b 1
)
"%NETDISK_PYTHON%" -c "import flask, requests; from playwright.sync_api import sync_playwright"
if errorlevel 1 (
  echo Login dependencies are missing. Run setup from README.md.
  pause
  exit /b 1
)
echo Open http://127.0.0.1:5001 in your browser.
echo Keep this window open. Press Ctrl+C to stop.
"%NETDISK_PYTHON%" main.py --web --port 5001
if errorlevel 1 pause
