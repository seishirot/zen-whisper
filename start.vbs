Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = scriptDir
logFile = scriptDir & "\start_debug.log"

shell.Run "cmd /c uv run zen-whisper < NUL >> """ & logFile & """ 2>&1", 0, False
