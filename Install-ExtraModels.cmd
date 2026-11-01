@echo off
setlocal
"%SystemRoot%\System32\chcp.com" 65001 >nul
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0scripts\inference\Install-ExtraModels.ps1" %*
set "INSTALL_RESULT=%ERRORLEVEL%"
pause
exit /b %INSTALL_RESULT%
