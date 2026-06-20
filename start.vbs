Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
quote = Chr(34)
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = scriptDir
logFile = scriptDir & "\start_debug.log"
venvExe = scriptDir & "\.venv\Scripts\zen-whisper.exe"

If fso.FileExists(venvExe) Then
    command = quote & venvExe & quote
Else
    command = "uv run zen-whisper"
End If

cmdExe = shell.ExpandEnvironmentStrings("%ComSpec%")
shell.Run quote & cmdExe & quote & " /d /c " & command & " < NUL >> " & quote & logFile & quote & " 2>&1", 0, False
