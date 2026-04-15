@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_pyinstaller.ps1" %*
exit /b %errorlevel%
