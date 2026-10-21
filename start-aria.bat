@echo off
title Aria
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo First start: setting up Aria. This takes a minute...
  python -m venv .venv
  if errorlevel 1 (
    echo.
    echo Python was not found. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" in the installer, then try again.
    pause
    exit /b 1
  )
)

call ".venv\Scripts\activate.bat"
echo Checking packages...
python -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo Installing packages failed. Check your internet connection and try again.
  pause
  exit /b 1
)

python app.py
pause
