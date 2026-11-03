' Lance Hyperdrive Remote sans fenetre console :
' demarre python en arriere-plan si besoin, puis ouvre le navigateur.
Option Explicit

Dim sh, fso, dir, url, pythonExe, already, i, http, status

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
url = "http://127.0.0.1:5050"
pythonExe = ResolvePython()

already = IsServerUp()
If Not already Then
  If pythonExe = "" Then
    MsgBox "Python introuvable. Installe Python ou ajoute-le au PATH.", vbExclamation, "Hyperdrive Remote"
    WScript.Quit 1
  End If
  sh.CurrentDirectory = dir
  ' 0 = fenetre cachee
  sh.Run """" & pythonExe & """ app.py", 0, False
  For i = 1 To 50
    WScript.Sleep 200
    If IsServerUp() Then Exit For
  Next
End If

sh.Run url, 1, False
WScript.Quit 0

Function IsServerUp()
  On Error Resume Next
  Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
  If Err.Number <> 0 Then
    Err.Clear
    Set http = CreateObject("MSXML2.XMLHTTP")
  End If
  http.setTimeouts 1000, 1000, 1000, 1000
  http.Open "GET", url, False
  http.Send
  status = http.Status
  If Err.Number = 0 And status >= 200 And status < 500 Then
    IsServerUp = True
  Else
    IsServerUp = False
  End If
  Err.Clear
  On Error GoTo 0
End Function

Function ResolvePython()
  Dim candidates, c, cmd, rc
  candidates = Array( _
    sh.ExpandEnvironmentStrings("%LocalAppData%\Python\pythoncore-3.14-64\python.exe"), _
    sh.ExpandEnvironmentStrings("%LocalAppData%\Programs\Python\Python314\python.exe"), _
    sh.ExpandEnvironmentStrings("%LocalAppData%\Programs\Python\Python313\python.exe"), _
    sh.ExpandEnvironmentStrings("%LocalAppData%\Programs\Python\Python312\python.exe"), _
    "C:\Users\joshu\AppData\Local\Microsoft\WindowsApps\python.exe" _
  )
  For Each c In candidates
    If fso.FileExists(c) Then
      ResolvePython = c
      Exit Function
    End If
  Next
  On Error Resume Next
  cmd = "cmd /c where python >nul 2>&1"
  rc = sh.Run(cmd, 0, True)
  If rc = 0 Then
    ResolvePython = "python"
  Else
    ResolvePython = ""
  End If
  On Error GoTo 0
End Function
