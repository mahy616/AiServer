param([Parameter(ValueFromRemainingArguments=$true)][string[]]$InstallArguments)
$ErrorActionPreference = 'Stop'
try {
    $root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
    $exe = 'C:\ProgramData\anaconda3\envs\trt_test_1013\python.exe'
    $prefs = Join-Path $root '.local\server-manager\launcher.json'
    if (Test-Path -LiteralPath $prefs) {
        $saved = Get-Content -Raw -LiteralPath $prefs -Encoding UTF8 | ConvertFrom-Json
        if ($saved.python) { $exe = $saved.python }
    }
    if (-not [IO.Path]::IsPathRooted($exe) -or -not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        Add-Type -AssemblyName System.Windows.Forms
        $picker = New-Object System.Windows.Forms.OpenFileDialog
        $picker.Title = 'Select the server Python 3.12 executable'
        $picker.Filter = 'Python executable (python.exe)|python.exe'
        if ($picker.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { exit 1 }
        $exe = $picker.FileName
    }
    $env:PYTHONUTF8 = '1'
    Set-Location -LiteralPath $root
    & $exe -u (Join-Path $root 'scripts\inference\Install-ExtraModels.py') --install-dependencies @InstallArguments
    exit $LASTEXITCODE
} catch {
    Write-Host 'Unable to launch. Check the copied files, saved Python path, and stopped AiServer.'
    exit 1
}
