@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 goto :err
  echo Installing dependencies...
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 goto :err
)

echo.
echo PC CONTROL: http://127.0.0.1:5000
echo For LAN access set PC_CONTROL_HOST, PC_CONTROL_PORT and PC_CONTROL_TOKEN
echo (see README.md) before running this file.
echo.
.venv\Scripts\python.exe server.py
pause
exit /b 0

:err
echo.
echo Installation failed. Make sure Python 3.10+ is installed and added to PATH.
pause
exit /b 1
