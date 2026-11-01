@echo off
call "%~dp0Install-ExtraModels.cmd" --models gemma12 %*
exit /b %ERRORLEVEL%
