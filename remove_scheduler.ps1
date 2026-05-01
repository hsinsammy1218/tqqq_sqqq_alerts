# Removes the three TQQQ/SQQQ alert scheduled tasks if present (safe no-op when missing).

$ErrorActionPreference = 'Continue'
$names = @(
    'TQQQ_SQQQ_Alerts_1000',
    'TQQQ_SQQQ_Alerts_1230',
    'TQQQ_SQQQ_Alerts_1530'
)

foreach ($name in $names) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "Removed scheduled task: $name"
    }
    else {
        Write-Host "Skipped (not found): $name"
    }
}

Write-Host 'Done.'
