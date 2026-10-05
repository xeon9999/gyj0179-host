@echo off
title GYJ-0179 Single-Slave Console
cd /d "%~dp0"
echo Starting GYJ-0179 Single-Slave Console...
echo.
python -m gyj0179 --gui-single
echo.
echo Program exited.
pause
