@echo off
rem ==================================================================
rem  Double-click this file to download by typing a name (no GUI).
rem ==================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
title BBJP Gallery Downloader (command line)

call "_bootstrap.bat"
if errorlevel 1 goto :error

echo.
echo  ================================================================
echo   BBJP Gallery Downloader
echo   For personal, lawful use only. Respect the site's terms,
echo   robots.txt and copyright. Adults-only content.
echo  ================================================================
echo.

set "NAME="
set /p "NAME=Enter the person's name to download: "
if "!NAME!"=="" (
    echo No name entered. Nothing to do.
    pause
    exit /b 1
)

echo.
echo Downloading all galleries for "!NAME!" ...
echo.
"%VENV_PY%" -m bbjp_downloader "!NAME!" -v
set "RC=!errorlevel!"

echo.
if "!RC!"=="0" (
    echo Finished. Files are in the "downloads\!NAME!" folder.
) else (
    echo Finished with errors ^(exit code !RC!^). See the messages above.
)
pause
exit /b !RC!

:error
echo.
echo Something went wrong. Read the messages above.
pause
exit /b 1
