@echo off
cd /d "%~dp0"
title WeChat Junshi - Self Check
echo ============================================================
echo   WeChat Junshi : SELF CHECK
echo   (checks only, does not touch WeChat)
echo ============================================================
echo.
python "%~dp0bot.py" --check
if errorlevel 1 (
  echo.
  echo python failed. Make sure python is on PATH and deps are installed:
  echo     pip install -r requirements.txt
)
echo.
pause
