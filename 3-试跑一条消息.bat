@echo off
cd /d "%~dp0"
title WeChat Junshi - Try One Message
python "%~dp0bot.py" --interactive
echo.
pause
