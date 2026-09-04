@echo off
rem ==================================================================
rem  Optional one-time setup. Installs Python dependencies into a
rem  local .venv so the app is ready to run. The other .bat files
rem  also do this automatically on first run, so this is optional.
rem ==================================================================
cd /d "%~dp0"
title BBJP Gallery Downloader - Setup

call "_bootstrap.bat"
if errorlevel 1 (
    echo.
    echo Setup failed. Read the messages above.
    pause
    exit /b 1
)

echo.
echo  Setup complete. You can now run:
echo    bbjp-downloader.bat   - the graphical app
echo    download.bat          - type a name in the console
echo.
pause
exit /b 0
