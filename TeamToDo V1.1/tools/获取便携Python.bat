@echo off
title 获取便携 Python 运行环境
cd /d "%~dp0"
set "DEST=%~dp0..\runtime\python"
set "ZIP=%TEMP%\py38.zip"
set "URL=https://www.python.org/ftp/python/3.8.10/python-3.8.10-embed-win32.zip"

echo.
echo ============================================================
echo   下载免安装的 Python 运行环境（约 7 MB）
echo ============================================================
echo.
echo 目标目录: %DEST%
echo.

if not exist "%DEST%" mkdir "%DEST%"

echo [1/3] 正在下载...
powershell -Command "$ProgressPreference='SilentlyContinue'; try { [Net.ServicePointManager]::SecurityProtocol = [Enum]::ToObject([Net.SecurityProtocolType], 3072) } catch {}; (New-Object System.Net.WebClient).DownloadFile('%URL%', '%ZIP%'); if(-not (Test-Path '%ZIP%')) { exit 1 }"
if errorlevel 1 (
    echo    PowerShell 下载失败，尝试 certutil ...
    certutil -urlcache -split -f "%URL%" "%ZIP%" >nul
)
if not exist "%ZIP%" (
    echo.
    echo [错误] 下载失败。请手动下载并解压到 runtime\python 目录:
    echo     %URL%
    pause
    exit /b 1
)

echo [2/3] 正在解压...
powershell -Command "$ProgressPreference='SilentlyContinue'; Add-Type -AssemblyName System.IO.Compression.FileSystem; [System.IO.Compression.ZipFile]::ExtractToDirectory('%ZIP%', '%DEST%')"
if errorlevel 1 (
    echo [错误] 解压失败，请手动解压 %ZIP% 到 %DEST%
    pause
    exit /b 1
)
del /f /q "%ZIP%" >nul 2>nul

echo [3/3] 完成
echo.
echo 现在可以运行「启动服务.bat」了。
echo.
pause
