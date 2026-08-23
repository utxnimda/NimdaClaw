@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-desktop.ps1" %*
exit /b %ERRORLEVEL%
