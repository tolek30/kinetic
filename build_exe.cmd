@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Run start.cmd once first to create the Python environment.
  pause
  exit /b 1
)

echo Installing build tools...
call .venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements-build.txt
if errorlevel 1 (
  echo Could not install PyInstaller build requirements.
  pause
  exit /b 1
)

echo Building the KINETIC desktop executable...
if not defined KINETIC_EXE_NAME set "KINETIC_EXE_NAME=KINETIC"
call .venv\Scripts\python.exe build_exe.py
if errorlevel 1 (
  echo Build failed. See the PyInstaller output above.
  pause
  exit /b 1
)

powershell -NoProfile -Command "$src = Join-Path '%CD%' 'dist\data'; $dest = Join-Path $env:LOCALAPPDATA 'KINETIC'; if ((Test-Path -LiteralPath (Join-Path $src 'config.json')) -and -not (Test-Path -LiteralPath (Join-Path $dest 'config.json'))) { New-Item -ItemType Directory -Path $dest -Force | Out-Null; Copy-Item -Path (Join-Path $src '*') -Destination $dest -Recurse -Force }; if (Test-Path -LiteralPath $src) { Remove-Item -LiteralPath $src -Recurse -Force }"
if errorlevel 1 (
  echo Warning: user settings could not be migrated to LocalAppData.
)

echo The executable is at dist\%KINETIC_EXE_NAME%.exe
pause
