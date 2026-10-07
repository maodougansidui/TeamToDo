' TeamToDo 后台静默启动（用于开机自启，不显示窗口）
Dim ws, fso, here, exe, py
Set ws  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
ws.CurrentDirectory = here

exe = here & "\TeamToDo.exe"
py  = here & "\runtime\python\python.exe"

If fso.FileExists(exe) Then
    ws.Run """" & exe & """ --no-browser", 0, False
ElseIf fso.FileExists(py) Then
    ws.Run """" & py & """ """ & here & "\server.py"" --no-browser", 0, False
Else
    ws.Run "cmd /c """ & here & "\启动服务.bat""", 7, False
End If
