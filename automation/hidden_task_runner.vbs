Option Explicit

' Run only AI YouTuber Growth's cycle without a console window.
' Exit with the child PowerShell exit code so Task Scheduler can report failures.
Dim shell, fso, root, cycleScript, powershellExe, command, exitCode, errorText, logFolder, logPath
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
cycleScript = fso.BuildPath(root, "automation\windows_cycle.ps1")
powershellExe = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
logFolder = fso.BuildPath(root, "logs")
logPath = fso.BuildPath(logFolder, "automation-launcher-" & Year(Now) & "-" & Right("0" & Month(Now), 2) & "-" & Right("0" & Day(Now), 2) & ".log")

Sub LogMessage(message)
    Dim stream
    On Error Resume Next
    If Not fso.FolderExists(logFolder) Then fso.CreateFolder logFolder
    Set stream = fso.OpenTextFile(logPath, 8, True)
    If Not (stream Is Nothing) Then
        stream.WriteLine Now & " [launcher] " & message
        stream.Close
    End If
    On Error GoTo 0
End Sub

If Not fso.FileExists(powershellExe) Then
    LogMessage "ERROR powershell.exe not found: " & powershellExe
    WScript.Quit 1
End If

If WScript.Arguments.Count > 0 Then
    If LCase(WScript.Arguments(0)) = "/selftest" Then
        command = Chr(34) & powershellExe & Chr(34) & " -NoProfile -NonInteractive -WindowStyle Hidden -Command " & Chr(34) & "exit 7" & Chr(34)
    Else
        LogMessage "ERROR unexpected launcher arguments"
        WScript.Quit 2
    End If
Else
    If Not fso.FileExists(cycleScript) Then
        LogMessage "ERROR cycle script not found: " & cycleScript
        WScript.Quit 1
    End If
    command = Chr(34) & powershellExe & Chr(34) & " -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & cycleScript & Chr(34)
End If

LogMessage "START hidden cycle (runner PID controlled by Task Scheduler)"
On Error Resume Next
Err.Clear
exitCode = shell.Run(command, 0, True)
If Err.Number <> 0 Then
    errorText = Err.Description
    Err.Clear
    On Error GoTo 0
    LogMessage "ERROR child launch failed: " & errorText
    WScript.Quit 1
End If
On Error GoTo 0
LogMessage "END child exit_code=" & exitCode
WScript.Quit exitCode
