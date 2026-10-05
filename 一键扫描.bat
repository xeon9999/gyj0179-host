@echo off
title GYJ-0179 Scan All Slaves
cd /d "%~dp0"
echo Scanning all GYJ-0179 slaves (auto baudrate 9600~115200)...
echo.
python -m gyj0179.scan_all
echo.
echo Scan finished.
pause
