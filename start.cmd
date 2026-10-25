@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python 3 is required. Install it from https://www.python.org/downloads/
    pause
    exit /b 1
  )
  set "PYTHON=python"
) else (
  py -3.13 --version >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON=py -3.13"
  ) else (
    set "PYTHON=py -3"
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating the KINETIC Python environment...
  %PYTHON% -m venv .venv
  if errorlevel 1 (
    echo Python environment setup failed.
    pause
    exit /b 1
  )
)

call .venv\Scripts\python.exe -c "import fastapi, uvicorn, websockets, multipart, PIL, webview" >nul 2>nul
if errorlevel 1 (
  echo Installing Python requirements...
  call .venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo Dependency installation failed.
    pause
    exit /b 1
  )
)

call .venv\Scripts\python.exe -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7399/api/health', timeout=1)" >nul 2>nul
if not errorlevel 1 (
  echo KINETIC is already running in its desktop window.
  pause
  exit /b 0
)

start "" /b .venv\Scripts\python.exe desktop.py
echo KINETIC desktop window is starting. Choose Host or Instance in the app.
pause
