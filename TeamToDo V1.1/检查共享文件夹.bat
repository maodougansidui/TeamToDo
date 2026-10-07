@echo off
title TeamToDo - 共享文件夹连通性检查
cd /d "%~dp0"
echo.
echo ============================================================
echo    检查共享文件夹是否可以访问
echo ============================================================
echo.
set "DBFOLDER="
set /p DBFOLDER=共享文件夹路径（直接回车使用 config.json 中的设置）: 

if not defined DBFOLDER (
    if exist "%~dp0config.json" (
        for /f "usebackq delims=" %%L in (`findstr /C:"\"folder\":" "%~dp0config.json"`) do set "CFGLINE=%%L"
    )
)
if defined CFGLINE (
    rem 提取冒号后引号中的内容
    set "DBFOLDER=%CFGLINE:*"=%"
    set "DBFOLDER=%DBFOLDER:~1%"
    set "DBFOLDER=%DBFOLDER:"=%"
    rem 去掉结尾逗号和空格，并把正斜杠换回反斜杠
    if "%DBFOLDER:~-1%"=="," set "DBFOLDER=%DBFOLDER:~0,-1%"
    set "DBFOLDER=%DBFOLDER: =%"
    set "DBFOLDER=%DBFOLDER:/=\%"
)

if not defined DBFOLDER (
    echo.
    echo [提示] 未填写路径，且 config.json 中没有配置共享文件夹。
    echo        可先运行「共享数据库设置.bat」完成配置。
    echo.
    pause
    exit /b 1
)

echo.
echo 正在访问: %DBFOLDER%
echo.
if not exist "%DBFOLDER%" (
    echo [失败] 无法访问该共享文件夹！
    echo.
    echo 请依次检查：
    echo   1. 电脑是否已连接公司内网（网线/内网 WiFi）
    echo   2. 该文件夹是否需要在"网上邻居"先登录账号
    echo   3. 路径是否填写正确（注意大小写和层级）
    echo   4. 是否拥有该文件夹的访问权限（找管理员开通）
    echo.
) else (
    echo [成功] 共享文件夹可以访问！
    echo.
    echo 目录内容（最多显示 10 条）：
    echo ------------------------------------------------------------
    dir /b "%DBFOLDER%" 2>nul | more +0
    echo ------------------------------------------------------------
)
echo.
pause
