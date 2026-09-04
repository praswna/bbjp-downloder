@echo off
rem ------------------------------------------------------------------
rem Shared bootstrap for the .bat launchers.
rem Finds Python, creates a local .venv, and installs dependencies once.
rem On success sets VENV_PY to the venv's python.exe and returns 0.
rem This file is meant to be CALLed, not double-clicked.
rem ------------------------------------------------------------------

rem --- Locate a Python 3 launcher -----------------------------------
set "PYLAUNCH="
where py >nul 2>nul && set "PYLAUNCH=py -3"
if not defined PYLAUNCH (
    where python >nul 2>nul && set "PYLAUNCH=python"
)
if not defined PYLAUNCH (
    echo.
    echo [!] Python 3 was not found on this computer.
    echo     Install it from https://www.python.org/downloads/
    echo     and make sure to tick "Add Python to PATH" during setup.
    exit /b 1
)

rem --- Create the virtual environment on first run ------------------
if not exist ".venv\Scripts\python.exe" (
    echo Creating a local Python environment ^(.venv^) ...
    %PYLAUNCH% -m venv .venv
    if errorlevel 1 (
        echo [!] Could not create the virtual environment.
        exit /b 1
    )
)
set "VENV_PY=.venv\Scripts\python.exe"

rem --- Install dependencies if they are missing ---------------------
"%VENV_PY%" -c "import requests, bs4, PIL" >nul 2>nul
if errorlevel 1 (
    echo Installing dependencies ^(first run only^) ...
    "%VENV_PY%" -m pip install --upgrade pip >nul 2>nul
    "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [!] Failed to install dependencies. Check your internet connection.
        exit /b 1
    )
)

exit /b 0
