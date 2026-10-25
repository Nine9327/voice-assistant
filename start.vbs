' Voice assistant launcher - no console window at all.
'
' Double-click this file:
'   1. starts assistant.py  (models, microphone, config page)
'   2. starts pet.py        (the desktop character)
' To stop everything, run stop-all.bat.
'
' Both children run under pythonw.exe so nothing flashes on screen;
' their output goes to .runtime\assistant.log and .runtime\pet.log.
'
' Launching goes through "cmd /c" on purpose. WScript.Shell.Run uses
' ShellExecute, which can pop an "Open File - Security Warning" dialog
' for the venv trampoline exe and then block forever waiting for a click
' (seen in the wild: assistant silently "never started"). cmd.exe is
' trusted, and cmd internally uses CreateProcess for the pythonw line,
' which skips the attachment check entirely - same path run.bat uses.

Option Explicit

Dim fso, sh, here, pyw, q, assistant, pet

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here

pyw = here & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(pyw) Then
    pyw = "pythonw.exe"   ' fall back to whatever is on PATH
End If

q = Chr(34)   ' a double quote, to keep the quoting below readable

assistant = here & "\assistant.py"
pet = here & "\pet.py"

If Not fso.FileExists(assistant) Then
    MsgBox "assistant.py not found next to start.vbs", 16, "Voice Assistant"
    WScript.Quit 1
End If

' 0 = hidden window, False = do not wait for it to finish.
' --no-browser: the config page is one right-click away on the pet, no need
' to hijack the browser every time the assistant boots.
'
' The whole command is wrapped in one extra pair of quotes: cmd /c strips the
' first and last quote when the line contains more than two, which would
' mangle "exe" "script" into exe" "script - the launch then fails with
' "'...' is not recognized" printed into the HIDDEN window, invisible.
' Wrapping keeps cmd's hands off the inner quotes.
sh.Run "cmd /c " & q & q & pyw & q & " " & q & assistant & q & " --no-browser" & q, 0, False

' Give the config server a head start so the pet connects on its first poll
WScript.Sleep 1500

If fso.FileExists(pet) Then
    sh.Run "cmd /c " & q & q & pyw & q & " " & q & pet & q & q, 0, False
End If
