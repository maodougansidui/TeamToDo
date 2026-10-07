@echo off
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
if exist "%STARTUP%\TeamToDo.lnk" (
    del /f /q "%STARTUP%\TeamToDo.lnk"
    echo [OK] 已取消开机自动启动
) else (
    echo 当前没有设置开机启动。
)
echo.
pause
