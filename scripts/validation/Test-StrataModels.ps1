param([Parameter(ValueFromRemainingArguments=$true)][string[]]$TestArguments)
$ErrorActionPreference = 'Stop'
try {
    $root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
    $exe = 'C:\ProgramData\anaconda3\envs\trt_test_1013\python.exe'
    $prefs = Join-Path $root '.local\server-manager\launcher.json'
    if (Test-Path -LiteralPath $prefs) {
        $saved = Get-Content -Raw -LiteralPath $prefs -Encoding UTF8 | ConvertFrom-Json
        if ($saved.python) { $exe = $saved.python }
    }
    if (-not [IO.Path]::IsPathRooted($exe) -or -not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'Absolute Python path is missing' }
    $env:PYTHONUTF8 = '1'
    Set-Location -LiteralPath $root
    & $exe -u (Join-Path $root 'scripts\validation\Test-Strata.py') --all --benchmark --pause @TestArguments
    exit $LASTEXITCODE
} catch {
    Write-Host 'Unable to test Strata models. Check the saved Python path, prepare both models and stop AiServer first.'
    exit 1
}
