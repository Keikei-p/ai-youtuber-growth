Option Explicit
' Windows Script Host launches PowerShell without a visible console.
' Wait for completion to return the real cycle exit code to Task Scheduler.
Dim shell, fs, repoRoot, cycleScript, powerShellExe, command, result
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
repoRoot = fs.GetParentFolderName(fs.GetParentFolderName(WScript.ScriptFullName))
cycleScript = fs.BuildPath(repoRoot, "automation\windows_cycle.ps1")
powerShellExe = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")

If Not fs.FileExists(powerShellExe) Then
    WScript.Quit 127
End If

If WScript.Arguments.Count = 0 Then
    If Not fs.FileExists(cycleScript) Then
        WScript.Quit 2
    End If
    command = Chr(34) & powerShellExe & Chr(34) & " -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & cycleScript & Chr(34)
ElseIf WScript.Arguments.Count = 1 Then
    If WScript.Arguments(0) <> "--probe" Then
        WScript.Quit 64
    End If
    ' Inert CI/PC probe. Never invokes scheduler.py or reads the user database.
    command = Chr(34) & powerShellExe & Chr(34) & " -NoProfile -NonInteractive -WindowStyle Hidden -Command " & Chr(34) & "exit 17" & Chr(34)
Else
    WScript.Quit 64
End If

On Error Resume Next
result = shell.Run(command, 0, True)
If Err.Number <> 0 Then
    WScript.Quit 1
End If
On Error GoTo 0
WScript.Quit result
