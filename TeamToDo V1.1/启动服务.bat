@echo off
setlocal enabledelayedexpansion
title TeamToDo 服务
cd /d "%~dp0"

rem ---------- 1. 打包好的主程序（无需 Python 环境） ----------
if exist "%~dp0TeamToDo.exe" (
    echo.
    echo ============================================================
    echo    TeamToDo 任务协同系统 - 正在启动
    echo ============================================================
    echo.
    echo [OK] 找到主程序 TeamToDo.exe，正在后台启动...
    echo       服务启动后，窗口会自动消失。
    echo       稍后请手动打开浏览器访问 http://127.0.0.1:8765
    echo       （或双击「打开任务页面.bat」）
    echo ------------------------------------------------------------
    start "" /b "%~dp0TeamToDo.exe" %*
    exit
)

rem ---------- 2. 随包携带的便携 Python（可选） ----------
if exist "%~dp0runtime\python\python.exe" (
    echo [OK] 使用便携 Python 运行环境
    start "" /b "%~dp0runtime\python\python.exe" "%~dp0server.py" %*
    exit
)

rem ---------- 3. 系统已安装的 Python ----------
for %%P in ("py -3" "python" "python3") do (
    %%P -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        echo [OK] 使用系统中的 Python: %%P
        start "" /b %%P "%~dp0server.py" %*
        exit
    )
)

echo [错误] 本机没有可用的运行环境，也没有找到 TeamToDo.exe
echo.
echo   请按以下任一方式处理：
echo   1. 把 TeamToDo.exe 复制到本目录后重新运行本脚本
echo   2. 运行 tools\获取便携Python.bat  自动下载免安装的运行环境
echo   3. 联系管理员安装 Python 3.6 及以上版本
echo.
pause
exit /b 1
