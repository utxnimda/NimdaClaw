@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0organize-media-directories.ps1" %*
exit /b %ERRORLEVEL%
