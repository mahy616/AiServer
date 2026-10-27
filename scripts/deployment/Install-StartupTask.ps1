param(
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$ConfigPath,
    [string]$TaskName = 'AiServer',
    [string]$RunAsUser = 'SYSTEM'
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$entry = Join-Path $repoRoot 'scripts/server/Run.py'
foreach ($path in @($PythonPath, $ConfigPath, $entry)) {
    if (-not [System.IO.Path]::IsPathRooted($path) -or -not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing absolute file: $path" }
    if ($path.Contains('"')) { throw 'Quotes are not allowed in task file paths' }
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { throw 'Task already exists; inspect its owner and stop it before updating' }
$action = New-ScheduledTaskAction -Execute $PythonPath -Argument ('"' + $entry + '" --config "' + $ConfigPath + '"') -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
if ($RunAsUser -eq 'SYSTEM') {
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
} else {
    $credential = Get-Credential -UserName $RunAsUser -Message 'Account must read Python/config/models and write .local. Credentials are stored by Windows Task Scheduler.'
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -User $credential.UserName -Password $credential.GetNetworkCredential().Password -RunLevel Highest | Out-Null
}
Write-Output "Registered $TaskName. Start-ScheduledTask after checking the target paths. Run.py owns the engine and tunnel; disable separate launch tasks."
