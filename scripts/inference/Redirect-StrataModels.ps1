param(
    [string]$StorageRoot = 'E:\AIModels\AiServer-Strata',
    [ValidateSet('both', 'qwen', 'deepseek')][string]$Family = 'both',
    [ValidateSet('auto', 'four-shard', 'six-shard')][string]$DeepSeekLayout = 'auto',
    [switch]$CheckOnly,
    [switch]$NoPause
)
$ErrorActionPreference = 'Stop'
$result = 1
$completed = @()
try {
    $root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
    if ($StorageRoot -notmatch '^[A-Za-z]:[\\/]') {
        throw 'StorageRoot must be an absolute local drive folder, e.g. E:\AIModels\AiServer-Strata'
    }
    $target = [IO.Path]::GetFullPath($StorageRoot)
    if (-not (Test-Path -LiteralPath $target -PathType Container)) {
        throw "Move the original model folders here first: $target"
    }

    # Check BOTH families before invoking either installer. Missing large weights
    # must produce an error instead of starting an unexpected download.
    $missing = @()
    $families = if ($Family -eq 'both') { @('qwen', 'deepseek') } else { @($Family) }
    $selectedDeepSeekLayout = 'four-shard'
    foreach ($modelFamily in $families) {
        $suffix = if ($modelFamily -eq 'deepseek') { '.deepseek' } else { '' }
        $manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $root "config\models\strata$suffix.source.json") | ConvertFrom-Json
        $folder = if ($modelFamily -eq 'deepseek') { 'models\deepseek41\IQ2_XXS' } else { 'models\IQ3_XXS' }
        $modelFiles = @($manifest.model_files)
        if ($modelFamily -eq 'deepseek') {
            $sixFiles = @()
            if ($manifest.model_file_sets) { $sixFiles = @($manifest.model_file_sets.'six-shard') }
            $foundSix = @($sixFiles | Where-Object {
                $_.filename -like '*-of-00006.gguf' -and
                ((Test-Path -LiteralPath (Join-Path (Join-Path $target $folder) $_.filename) -PathType Leaf) -or
                 (Test-Path -LiteralPath (Join-Path (Join-Path $target $folder) ($_.filename + '.download')) -PathType Leaf))
            }).Count -gt 0
            if ($DeepSeekLayout -eq 'six-shard' -or ($DeepSeekLayout -eq 'auto' -and $foundSix)) {
                if ($sixFiles.Count -ne 7) { throw 'The pinned six-shard manifest is missing. Update the complete program payload first.' }
                $selectedDeepSeekLayout = 'six-shard'
                $modelFiles = $sixFiles
            }
            Write-Host "DeepSeek weight layout: $selectedDeepSeekLayout"
        }
        foreach ($row in $modelFiles) {
            $file = Join-Path (Join-Path $target $folder) $row.filename
            if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
                $missing += "Missing: $file"
            } elseif ((Get-Item -LiteralPath $file).Length -ne [long]$row.size) {
                $missing += "Wrong size: $file (expected $($row.size) bytes)"
            }
        }
    }
    if ($missing.Count -gt 0) {
        $missing | ForEach-Object { Write-Host $_ }
        throw 'Model files are incomplete. Nothing was redirected. Keep the original filenames and folder layout.'
    }
    Write-Host ("Selected model files are present: $target (" + ($families -join ', ') + ')')
    if ($CheckOnly) {
        Write-Host 'File names and sizes passed. No configuration changed; SHA256 and model loading have not been checked.'
    } else {
        $exe = 'C:\ProgramData\anaconda3\envs\trt_test_1013\python.exe'
        $baseConfig = Join-Path $root 'config\server.local.json'
        $prefs = Join-Path $root '.local\server-manager\launcher.json'
        if (Test-Path -LiteralPath $prefs -PathType Leaf) {
            $saved = Get-Content -Raw -Encoding UTF8 -LiteralPath $prefs | ConvertFrom-Json
            if ($saved.python) { $exe = $saved.python }
            if ($saved.config) { $baseConfig = $saved.config }
        }
        if (-not [IO.Path]::IsPathRooted($exe) -or -not (Test-Path -LiteralPath $exe -PathType Leaf)) {
            throw 'Absolute Python path is missing. Save the server Python path in ServerManager first.'
        }
        if (-not [IO.Path]::IsPathRooted($baseConfig)) { $baseConfig = Join-Path $root $baseConfig }
        if (-not (Test-Path -LiteralPath $baseConfig -PathType Leaf)) {
            $baseConfig = Join-Path $root 'config\server.ollama.example.json'
        }
        $env:PYTHONUTF8 = '1'
        Set-Location -LiteralPath $root
        Write-Host 'Keep AiServer and its installers stopped. Weights will be SHA256-checked; packs will be rebuilt.'
        Write-Host 'LOCAL FILES ONLY. Missing runtime/dependencies/MTP files cause an error; no downloads or pip install.'
        foreach ($modelFamily in $families) {
            Write-Host "Redirecting $modelFamily ..."
            $layoutArguments = @()
            if ($modelFamily -eq 'deepseek') { $layoutArguments = @('--deepseek-layout', $selectedDeepSeekLayout) }
            & $exe -u (Join-Path $root 'scripts\inference\Install-Strata.py') --family $modelFamily --server-config $baseConfig --storage-root $target --offline @layoutArguments
            if ($LASTEXITCODE -ne 0) { throw "$modelFamily preparation failed (exit $LASTEXITCODE). See the installer output and .local\strata\install logs." }
            $completed += $modelFamily
        }
        Write-Host 'Selected models redirected. Reopen ServerManager, reload the desired Strata configuration and run startup checks.'
    }
    $result = 0
} catch {
    Write-Host ("Redirect failed: " + $_.Exception.Message)
    if ($completed.Count -gt 0) {
        Write-Host ("Already redirected successfully: " + ($completed -join ', ') + '. The two models are published independently; fix the failure and rerun.')
    }
} finally {
    if (-not $NoPause) { Read-Host 'Press Enter to close' | Out-Null }
}
exit $result
