@echo off
cd /d "%~dp0"
set "PORT=8765"
if exist "%~dp0port.txt" (
    set /p PORT=<"%~dp0port.txt"
)
echo 正在打开 http://127.0.0.1:%PORT%
start "" "http://127.0.0.1:%PORT%"
