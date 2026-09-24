Option Explicit

Dim shell, fso, root, pythonw, launcher, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = fso.BuildPath(root, ".venv\Scripts\pythonw.exe")
launcher = fso.BuildPath(root, "web_launcher.pyw")

If Not fso.FileExists(pythonw) Then
    MsgBox "Python仮想環境が見つかりません: " & pythonw, vbCritical, "AI YouTuber"
    WScript.Quit 1
End If

If Not fso.FileExists(launcher) Then
    MsgBox "Webランチャーが見つかりません: " & launcher, vbCritical, "AI YouTuber"
    WScript.Quit 1
End If

command = Chr(34) & pythonw & Chr(34) & " " & Chr(34) & launcher & Chr(34)
shell.Run command, 0, False
