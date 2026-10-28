@echo off
setlocal
"%SystemRoot%\System32\chcp.com" 65001 >nul
set "AISERVER_CONTEXT_128K_ROOT=%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -STA -Command "$ErrorActionPreference='Stop'; try { $root=$env:AISERVER_CONTEXT_128K_ROOT; $exe='C:\ProgramData\anaconda3\envs\trt_test_1013\python.exe'; $prefs=Join-Path $root '.local\server-manager\launcher.json'; if(Test-Path -LiteralPath $prefs){$saved=Get-Content -Raw -LiteralPath $prefs -Encoding UTF8 | ConvertFrom-Json; if($saved.python){$exe=$saved.python}}; if(-not [IO.Path]::IsPathRooted($exe) -or -not(Test-Path -LiteralPath $exe -PathType Leaf)){Add-Type -AssemblyName System.Windows.Forms; $picker=New-Object System.Windows.Forms.OpenFileDialog; $picker.Title='Select the server Python 3.12 executable'; $picker.Filter='Python executable (python.exe)|python.exe'; if($picker.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK){exit 1}; $exe=$picker.FileName}; $env:PYTHONUTF8='1'; $script=Join-Path $root 'scripts\validation\Tune-ModelContexts.py'; Set-Location -LiteralPath $root; & $exe -u $script --context 131072 --apply; exit $LASTEXITCODE } catch { Write-Host 'Unable to launch. Check the copied files, saved Python path, and stopped AiServer.'; exit 1 }"
set "CONTEXT_128K_RESULT=%ERRORLEVEL%"
pause
exit /b %CONTEXT_128K_RESULT%
