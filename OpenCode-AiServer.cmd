@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
"C:\ProgramData\anaconda3\envs\trt_test_1013\python.exe" -u "%~dp0scripts\client\OpenCode.py" %*
set "OPENCODE_RESULT=%ERRORLEVEL%"
if not "%OPENCODE_RESULT%"=="0" pause
exit /b %OPENCODE_RESULT%
