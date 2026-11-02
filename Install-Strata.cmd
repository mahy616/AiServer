@echo off
setlocal
"%SystemRoot%\System32\chcp.com" 65001 >nul
cd /d "%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\inference\Install-Strata.ps1" %*
exit /b %errorlevel%
