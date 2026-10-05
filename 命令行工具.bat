@echo off
title GYJ-0179 Command Line Tool
cd /d "%~dp0"
echo === GYJ-0179 Command Line Tool ===
echo Examples:
echo   python -m gyj0179.cli COM3 status
echo   python -m gyj0179.cli COM3 relay 1 on
echo   python -m gyj0179.cli COM3 di
echo   python -m gyj0179.cli COM3 param read 0
echo.
echo Type 'exit' to close this window.
echo.
cmd /k
