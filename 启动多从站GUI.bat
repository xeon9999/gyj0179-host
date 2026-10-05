@echo off
title GYJ-0179 Multi-Slave Monitor
cd /d "%~dp0"
echo Starting GYJ-0179 Multi-Slave Monitor...
echo.
python -m gyj0179 --gui-multi
echo.
echo Program exited.
pause
