@echo off
call "%~dp0Install-ExtraModels.cmd" --models gemma26 %*
exit /b %ERRORLEVEL%
