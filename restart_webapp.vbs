Option Explicit

Dim shell, fso, root, pythonw, launcher, command
Dim wmi, processes, process

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)

Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set processes = wmi.ExecQuery("SELECT * FROM Win32_Process WHERE Name='pythonw.exe' OR Name='python.exe'")

For Each process In processes
    On Error Resume Next
    If InStr(1, process.CommandLine, "web_launcher.pyw", vbTextCompare) > 0 _
       Or InStr(1, process.CommandLine, "webapp.py", vbTextCompare) > 0 Then
        process.Terminate
    End If
    On Error GoTo 0
Next

WScript.Sleep 1500

pythonw = fso.BuildPath(root, ".venv\Scripts\pythonw.exe")
launcher = fso.BuildPath(root, "web_launcher.pyw")

If Not fso.FileExists(pythonw) Then
    MsgBox "Python仮想環境が見つかりません: " & pythonw, vbCritical, "AI YouTuber"
    WScript.Quit 1
End If

command = Chr(34) & pythonw & Chr(34) & " " & Chr(34) & launcher & Chr(34)
shell.Run command, 0, False
