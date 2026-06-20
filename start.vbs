Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
quote = Chr(34)
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = scriptDir
logFile = scriptDir & "\start_debug.log"
launcherLogFile = scriptDir & "\start_vbs.log"
venvExe = scriptDir & "\.venv\Scripts\zen-whisper.exe"

Sub LogStart(message)
    Dim logFileHandle
    Dim line
    On Error Resume Next
    line = CStr(Now) & " [start.vbs] " & CStr(message)
    Set logFileHandle = fso.OpenTextFile(launcherLogFile, 8, True, 0)
    logFileHandle.WriteLine line
    logFileHandle.Close
    On Error GoTo 0
End Sub

If fso.FileExists(venvExe) Then
    command = quote & venvExe & quote
    LogStart "launching venv executable: " & venvExe
    shell.Run command, 0, False
Else
    command = "uv run zen-whisper"
    LogStart "venv executable not found; falling back to: " & command

    cmdExe = shell.ExpandEnvironmentStrings("%ComSpec%")
    shell.Run quote & cmdExe & quote & " /d /c " & command & " < NUL >> " & quote & logFile & quote & " 2>&1", 0, False
End If
