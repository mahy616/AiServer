param([string]$ConfigPath)
$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$env:PYTHONPATH = Join-Path $repoRoot 'server/src'
if ($ConfigPath) {
    & 'C:\ProgramData\anaconda3\envs\trt_test_1013\python.exe' -m aiserver --config ([IO.Path]::GetFullPath($ConfigPath)) serve
} else {
    & 'C:\ProgramData\anaconda3\envs\trt_test_1013\python.exe' -m aiserver --config (Join-Path $repoRoot 'config/server.example.json') serve
}
exit $LASTEXITCODE
