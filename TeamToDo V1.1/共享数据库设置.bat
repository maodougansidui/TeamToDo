@echo off
title TeamToDo - 共享数据库设置
cd /d "%~dp0"
echo.
echo ============================================================
echo    设置共享数据库（多人共用同一份数据）
echo ============================================================
echo.
echo 请填写共享文件夹路径（只需填到文件夹，不含文件名），例如：
echo     \\10.23.181.219\维修三处\99维修三处管理平台数据库
echo     \\fileserver\共享目录
echo     Z:\TeamToDo
echo.
echo 直接回车则恢复为单机模式（数据存在本目录 tasks.db）
echo.
set /p DBFOLDER=共享文件夹路径: 

rem JSON 里反斜杠是转义符，必须换成正斜杠，否则生成的 config.json 无法解析
if defined DBFOLDER set "DBFOLDER=%DBFOLDER:\=/%"
rem 去掉结尾的斜杠
if defined DBFOLDER if "%DBFOLDER:~-1%"=="/" set "DBFOLDER=%DBFOLDER:~0,-1%"

if not defined DBFOLDER (
    > "%~dp0config.json" echo {
    >> "%~dp0config.json" echo   "database": {
    >> "%~dp0config.json" echo     "folder": "",
    >> "%~dp0config.json" echo     "file": "tasks.db"
    >> "%~dp0config.json" echo   }
    >> "%~dp0config.json" echo }
    echo.
    echo [OK] 已恢复为单机模式
) else (
    > "%~dp0config.json" echo {
    >> "%~dp0config.json" echo   "database": {
    >> "%~dp0config.json" echo     "folder": "%DBFOLDER%",
    >> "%~dp0config.json" echo     "file": "tasks.db"
    >> "%~dp0config.json" echo   }
    >> "%~dp0config.json" echo }
    echo.
    echo [OK] 已写入 config.json
    echo     共享文件夹: %DBFOLDER%
    echo     数据库文件: %DBFOLDER%/tasks.db
    echo.
    echo 注意: 共享盘模式会自动使用兼容的日志方式（不用 WAL），
    echo       多人同时保存时偶有等待属正常现象。
)
echo.
pause
