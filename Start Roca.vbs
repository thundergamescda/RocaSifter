Option Explicit
Dim shell, files, folder, python, script, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
python = files.BuildPath(folder, ".venv\Scripts\pythonw.exe")
script = files.BuildPath(folder, "station_desktop.py")
If Not files.FileExists(python) Or Not files.FileExists(script) Then
    MsgBox "Run Install dependencies.cmd in the complete application folder first.", vbExclamation, "Roca Station"
    WScript.Quit 1
End If
shell.CurrentDirectory = folder
command = Chr(34) & python & Chr(34) & " " & Chr(34) & script & Chr(34)
On Error Resume Next
shell.Run command, 0, False
If Err.Number <> 0 Then
    MsgBox "Roca could not start. Run Install dependencies.cmd and try again.", vbExclamation, "Roca Station"
    WScript.Quit 1
End If
