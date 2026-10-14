@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
set "tool_exit=%errorlevel%"
if not "%tool_exit%"=="0" pause
exit /b %tool_exit%
