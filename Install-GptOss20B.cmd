@echo off
call "%~dp0Install-ExtraModels.cmd" --models gptoss %*
exit /b %ERRORLEVEL%
