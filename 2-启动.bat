@echo off
cd /d "%~dp0"
title WeChat Junshi - Running
echo ============================================================
echo   WeChat Junshi : AUTO REPLY
echo.
echo   Stop        : press Ctrl+C
echo   Keep the WeChat main window open while running
echo ============================================================
echo.
python "%~dp0bot.py"
echo.
echo Program exited.
pause
