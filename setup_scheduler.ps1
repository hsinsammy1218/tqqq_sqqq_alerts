# Creates daily Windows Task Scheduler jobs that run run_bot.ps1 at US-market-friendly times.
# Alert-only: no broker integration. May require "Run as administrator" if task registration fails.

$ErrorActionPreference = 'Stop'

$runBotPath = Join-Path $PSScriptRoot 'run_bot.ps1'
if (-not (Test-Path -LiteralPath $runBotPath)) {
    Write-Error "Missing run_bot.ps1 next to this script: $runBotPath"
    exit 1
}

$psArgs = "-ExecutionPolicy Bypass -File `"$runBotPath`""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $psArgs

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

$definitions = @(
    @{ Name = 'TQQQ_SQQQ_Alerts_1000'; At = '10:00AM' },
    @{ Name = 'TQQQ_SQQQ_Alerts_1230'; At = '12:30PM' },
    @{ Name = 'TQQQ_SQQQ_Alerts_1530'; At = '3:30PM' }
)

foreach ($def in $definitions) {
    $trigger = New-ScheduledTaskTrigger -Daily -At $def.At
    Register-ScheduledTask `
        -TaskName $def.Name `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Force | Out-Null
    Write-Host "Registered scheduled task: $($def.Name) daily at $($def.At)"
}

Write-Host "Done. Tasks invoke: powershell.exe -ExecutionPolicy Bypass -File `"$runBotPath`""
