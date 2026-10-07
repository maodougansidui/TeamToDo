@echo off
cd /d "%~dp0"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS=%~dp0后台启动.vbs"

if not exist "%VBS%" (
    echo [错误] 缺少 后台启动.vbs
    pause
    exit /b 1
)

> "%TEMP%\tt_mk.vbs" echo Set ws = CreateObject("WScript.Shell")
>> "%TEMP%\tt_mk.vbs" echo Set sc = ws.CreateShortcut("%STARTUP%\TeamToDo.lnk")
>> "%TEMP%\tt_mk.vbs" echo sc.TargetPath = "wscript.exe"
>> "%TEMP%\tt_mk.vbs" echo sc.Arguments = Chr(34) ^& "%VBS%" ^& Chr(34)
>> "%TEMP%\tt_mk.vbs" echo sc.WorkingDirectory = "%~dp0"
>> "%TEMP%\tt_mk.vbs" echo sc.Description = "TeamToDo 任务协同服务"
>> "%TEMP%\tt_mk.vbs" echo sc.Save

cscript //nologo "%TEMP%\tt_mk.vbs"
del /f /q "%TEMP%\tt_mk.vbs" >nul 2>nul

if errorlevel 1 (
    echo [错误] 创建快捷方式失败
) else (
    echo.
    echo [OK] 已设置开机自动启动
    echo     快捷方式: %STARTUP%\TeamToDo.lnk
    echo.
    echo 提示: 开机启动为后台静默模式，不会弹出浏览器。
    echo       需要查看任务时，双击「打开任务页面.bat」即可。
)
echo.
pause
