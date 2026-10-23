Option Explicit
Dim shell, files, folder, python, script, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
python = files.BuildPath(folder, ".venv\Scripts\pythonw.exe")
script = files.BuildPath(folder, "roca_update.py")
If Not files.FileExists(python) Or Not files.FileExists(script) Then
    shell.Run Chr(34) & files.BuildPath(folder, "Update Roca.cmd") & Chr(34), 1, False
    WScript.Quit
End If
command = Chr(34) & python & Chr(34) & " " & Chr(34) & script & Chr(34)
shell.Run command, 0, False
