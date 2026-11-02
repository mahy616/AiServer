param([Parameter(ValueFromRemainingArguments=$true)][string[]]$InstallArguments)
$ErrorActionPreference = 'Stop'
try {
    $root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
    $exe = 'C:\ProgramData\anaconda3\envs\trt_test_1013\python.exe'
    $prefs = Join-Path $root '.local\server-manager\launcher.json'
    $baseConfig = Join-Path $root 'config\server.local.json'
    if (Test-Path -LiteralPath $prefs) {
        $saved = Get-Content -Raw -LiteralPath $prefs -Encoding UTF8 | ConvertFrom-Json
        if ($saved.python) { $exe = $saved.python }
        if ($saved.config) { $baseConfig = $saved.config }
    }
    if (-not (Test-Path -LiteralPath $baseConfig)) { $baseConfig = Join-Path $root 'config\server.ollama.example.json' }
    if (-not [IO.Path]::IsPathRooted($exe) -or -not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'Absolute Python path is missing' }
    $env:PYTHONUTF8 = '1'
    Set-Location -LiteralPath $root
    & $exe -u (Join-Path $root 'scripts\inference\Install-Strata.py') --server-config $baseConfig --install-dependencies --pause @InstallArguments
    exit $LASTEXITCODE
} catch {
    Write-Host 'Unable to prepare Strata. Check the saved Python path and stop AiServer first.'
    exit 1
}
