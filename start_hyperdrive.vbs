' Always starts a CLEAN Hyperdrive Remote server (kills stale python on 5050), then opens the browser.
Option Explicit

Dim sh, fso, dir, ps1, cmd, rc

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
ps1 = dir & "\launch.ps1"

If Not fso.FileExists(ps1) Then
  MsgBox "launch.ps1 introuvable dans:" & vbCrLf & dir, vbCritical, "Hyperdrive Remote"
  WScript.Quit 1
End If

cmd = "powershell -NoProfile -ExecutionPolicy Bypass -File """ & ps1 & """"
rc = sh.Run(cmd, 0, True)
WScript.Quit rc
