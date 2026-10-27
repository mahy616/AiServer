@echo off
setlocal
set "AISERVER_BUILD_ROOT=%~dp0"
powershell.exe -NoProfile -STA -Command "Add-Type -AssemblyName System.Windows.Forms; $root=$env:AISERVER_BUILD_ROOT; $exe='C:\ProgramData\anaconda3\envs\trt_test_1013\python.exe'; $prefs=Join-Path $root '.local\server-manager\launcher.json'; if(Test-Path -LiteralPath $prefs){try{$saved=Get-Content -Raw -LiteralPath $prefs -Encoding UTF8 | ConvertFrom-Json; if($saved.python){$exe=$saved.python}}catch{}}; if(-not(Test-Path -LiteralPath $exe -PathType Leaf)){$picker=New-Object System.Windows.Forms.OpenFileDialog; $picker.Title='Select Python 3.12 python.exe'; $picker.Filter='Python executable (python.exe)|python.exe'; if($picker.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK){exit 0}; $exe=$picker.FileName}; $script=Join-Path $root 'scripts\deployment\Build-Update.py'; & $exe -u $script --pause"
endlocal
