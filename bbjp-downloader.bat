@echo off
rem ==================================================================
rem  Double-click this file to open the graphical downloader.
rem ==================================================================
cd /d "%~dp0"
title BBJP Gallery Downloader

call "_bootstrap.bat"
if errorlevel 1 goto :error

echo Starting the app ...
"%VENV_PY%" -m bbjp_downloader --gui
if errorlevel 1 goto :error
exit /b 0

:error
echo.
echo Something went wrong. Read the messages above.
pause
exit /b 1
