# Creates weekday Windows Task Scheduler jobs that run run_bot.ps1 at US-market-friendly times.
# run_bot.ps1 passes --market-hours-only so holidays and off-hours runs exit without fetching data.
# Alert-only: no broker integration. May require "Run as administrator" if task registration fails.

$ErrorActionPreference = 'Stop'

$runBotPath = Join-Path $PSScriptRoot 'run_bot.ps1'
if (-not (Test-Path -LiteralPath $runBotPath)) {
    Write-Error "Missing run_bot.ps1 next to this script: $runBotPath"
    exit 1
}

# Scheduled tasks often run with a minimal PATH; bare "powershell.exe" can fail with HRESULT 0x80070002.
$runBotFull = (Resolve-Path -LiteralPath $runBotPath).Path
$powershellExe = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $powershellExe)) {
    Write-Error "PowerShell not found at $powershellExe"
    exit 1
}

$psArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$runBotFull`""
$action = New-ScheduledTaskAction -Execute $powershellExe -Argument $psArgs -WorkingDirectory $PSScriptRoot

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

$taskUser = if ($env:USERDOMAIN) { "$($env:USERDOMAIN)\$($env:USERNAME)" } else { $env:USERNAME }
Write-Host "Registering tasks for user: $taskUser"

$definitions = @(
    @{ Name = 'TQQQ_SQQQ_Alerts_1000'; At = '10:00AM' },
    @{ Name = 'TQQQ_SQQQ_Alerts_1230'; At = '12:30PM' },
    @{ Name = 'TQQQ_SQQQ_Alerts_1530'; At = '3:30PM' }
)

function Register-AlertTask {
    param(
        [string]$TaskName,
        [string]$At,
        $TaskAction,
        $TaskSettings,
        [string]$UserId
    )

    $trigger = New-ScheduledTaskTrigger `
        -Weekly `
        -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
        -At $At

    try {
        $s4uPrincipal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType S4U -RunLevel Limited
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $TaskAction `
            -Trigger $trigger `
            -Settings $TaskSettings `
            -Principal $s4uPrincipal `
            -Force | Out-Null
        Write-Host "Registered scheduled task: $TaskName weekdays at $At (LogonType=S4U)"
        return
    }
    catch {
        Write-Warning "S4U registration failed for $TaskName ($($_.Exception.Message)). Trying Interactive mode."
    }

    try {
        $interactivePrincipal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $TaskAction `
            -Trigger $trigger `
            -Settings $TaskSettings `
            -Principal $interactivePrincipal `
            -Force | Out-Null
        Write-Host "Registered scheduled task: $TaskName weekdays at $At (LogonType=Interactive)"
        return
    }
    catch {
        Write-Error @"
Failed to register $TaskName in both S4U and Interactive modes.
Last error: $($_.Exception.Message)
Try re-running this script from an elevated PowerShell session (Run as administrator).
"@
        exit 1
    }
}

foreach ($def in $definitions) {
    Register-AlertTask `
        -TaskName $def.Name `
        -At $def.At `
        -TaskAction $action `
        -TaskSettings $settings `
        -UserId $taskUser
}

Write-Host "Done. Tasks invoke (Mon-Fri only, no weekends):"
Write-Host "  Execute: $powershellExe"
Write-Host "  Args:    $psArgs"
Write-Host "Verify weekday schedule:"
Write-Host "  Get-ScheduledTask -TaskName TQQQ_SQQQ_Alerts_1000 | Get-ScheduledTaskInfo"
Write-Host "  schtasks /Query /TN TQQQ_SQQQ_Alerts_1000 /V /FO LIST | findstr /I \"Day\""
