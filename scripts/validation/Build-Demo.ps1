$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$env:DOTNET_CLI_HOME = Join-Path $repoRoot '.local/dotnet'
$env:DOTNET_SKIP_FIRST_TIME_EXPERIENCE = '1'
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$project = Join-Path $repoRoot 'tools/ClientDemo/ClientDemo.csproj'
dotnet restore $project --configfile (Join-Path $repoRoot 'tools/ClientDemo/NuGet.Config') --verbosity quiet
if ($LASTEXITCODE -ne 0) { throw 'Demo restore failed' }
dotnet build $project --no-restore --nologo --verbosity quiet
if ($LASTEXITCODE -ne 0) { throw 'Demo build failed' }
