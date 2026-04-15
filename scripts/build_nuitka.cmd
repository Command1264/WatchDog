@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_nuitka.ps1" %*
exit /b %errorlevel%
