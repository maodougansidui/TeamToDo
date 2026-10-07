@echo off
title 停止 TeamToDo 服务
cd /d "%~dp0"
echo.
echo 正在停止 TeamToDo 服务...
echo.

taskkill /F /IM TeamToDo.exe >nul 2>nul
if not errorlevel 1 echo   [OK] 已结束 TeamToDo.exe

set "PORT=8765"
if exist "%~dp0port.txt" (
    set /p PORT=<"%~dp0port.txt"
)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%" ^| findstr LISTENING') do (
    echo   结束占用端口 %PORT% 的进程 PID=%%a
    taskkill /F /PID %%a >nul 2>nul
)

rem 兼容以 python 直接运行 server.py 的情况：
rem 只结束命令行里包含 server.py 的进程，避免误伤其它 Python 程序
wmic process where "name='python.exe' and commandline like '%%server.py%%'" call terminate >nul 2>nul

echo.
echo 操作完成。
echo.
pause
