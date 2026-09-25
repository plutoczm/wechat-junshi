@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\unregister_autostart.ps1"
if errorlevel 1 (
  echo.
  echo Autostart removal failed.
  pause
  exit /b 1
)
pause
