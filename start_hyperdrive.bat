@echo off
REM Compat: redirige vers le lanceur silencieux (.vbs), sans console visible.
start "" wscript.exe "%~dp0start_hyperdrive.vbs"
exit /b 0
