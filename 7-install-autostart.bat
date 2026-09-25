@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\register_autostart.ps1"
if errorlevel 1 (
  echo.
  echo Autostart setup failed.
  pause
  exit /b 1
)
pause
