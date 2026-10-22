' Voice assistant launcher - no console window at all.
'
' Double-click this file:
'   1. starts assistant.py  (models, microphone, config page)
'   2. starts pet.py        (the desktop character)
' To stop everything, run stop-all.bat.
'
' Both children are launched with pythonw.exe, so nothing flashes on screen;
' their output goes to .runtime\assistant.log and .runtime\pet.log.

Option Explicit

Dim fso, sh, here, pyw, assistant, pet

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here

pyw = "D:\miniconda3\envs\voice-assistant\pythonw.exe"
If Not fso.FileExists(pyw) Then
    pyw = "pythonw.exe"   ' fall back to whatever is on PATH
End If

assistant = here & "\assistant.py"
pet = here & "\pet.py"

If Not fso.FileExists(assistant) Then
    MsgBox "assistant.py not found next to start.vbs", 16, "Voice Assistant"
    WScript.Quit 1
End If

' 0 = hidden window, False = do not wait for it to finish.
' --no-browser: the config page is one right-click away on the pet, no need
' to hijack the browser every time the assistant boots.
sh.Run """" & pyw & """ """ & assistant & """ --no-browser", 0, False

' Give the config server a head start so the pet connects on its first poll
WScript.Sleep 1500

If fso.FileExists(pet) Then
    sh.Run """" & pyw & """ """ & pet & """", 0, False
End If
