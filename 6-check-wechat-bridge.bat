@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv-mobile\Scripts\python.exe" (
  echo Run 4-install-mobile-workbench.bat first.
  pause
  exit /b 1
)
".venv-mobile\Scripts\python.exe" -m mobile.selfcheck
pause
