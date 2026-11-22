' JamMan Card Tool ohne Konsolenfenster starten (Windows).
' Wird von der Verknuepfung auf dem Desktop aufgerufen.
' Angelegt am 22.11.2026, damit beim Start kein schwarzes Fenster aufblitzt.
Dim fso, sh, ordner
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
ordner = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = ordner
sh.Run """" & ordner & "\JamMan-Windows.bat""", 0, False
