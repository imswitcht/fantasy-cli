@echo off
cd /d "%~dp0"
python ff.py tui
if errorlevel 1 (
    echo.
    echo ff tui exited with an error - see above.
    pause
)
