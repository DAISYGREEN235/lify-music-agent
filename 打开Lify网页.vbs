Option Explicit
Dim shell, fso, root, python
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root
python = fso.BuildPath(root, ".venv\Scripts\pythonw.exe")
If Not fso.FileExists(python) Then
  MsgBox "Python environment missing: .venv", 48, "Lify"
  WScript.Quit 1
End If
shell.Run Chr(34) & python & Chr(34) & " -m lify.web --open", 0, False
