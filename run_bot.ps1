# Alert-only scheduled runner: invokes main.py with console summary (no technical block).
# Logs stdout/stderr from Python to logs/scheduler.log alongside START/END markers.

$ErrorActionPreference = 'Stop'
$ProjectRoot = $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

$logsDir = Join-Path $ProjectRoot 'logs'
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
$logFile = Join-Path $logsDir 'scheduler.log'

function Write-SchedulerLog {
    param([string]$Message)
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -LiteralPath $logFile -Value "[$ts] $Message" -Encoding UTF8
}

Write-SchedulerLog 'START run_bot.ps1'

$python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    Write-SchedulerLog "ERROR: Missing venv Python at $python"
    exit 1
}

$stdoutTmp = Join-Path $logsDir '_scheduler_stdout.tmp'
$stderrTmp = Join-Path $logsDir '_scheduler_stderr.tmp'
Remove-Item -LiteralPath $stdoutTmp, $stderrTmp -ErrorAction SilentlyContinue

$exitCode = 1
try {
    $p = Start-Process `
        -FilePath $python `
        -ArgumentList @('main.py', '--no-technical') `
        -WorkingDirectory $ProjectRoot `
        -Wait `
        -PassThru `
        -NoNewWindow `
        -RedirectStandardOutput $stdoutTmp `
        -RedirectStandardError $stderrTmp
    if (Test-Path -LiteralPath $stdoutTmp) {
        Get-Content -LiteralPath $stdoutTmp -ErrorAction SilentlyContinue | Add-Content -LiteralPath $logFile -Encoding UTF8
    }
    if (Test-Path -LiteralPath $stderrTmp) {
        Get-Content -LiteralPath $stderrTmp -ErrorAction SilentlyContinue | Add-Content -LiteralPath $logFile -Encoding UTF8
    }
    $exitCode = $p.ExitCode
}
catch {
    Write-SchedulerLog "ERROR: $($_.Exception.Message)"
    $exitCode = 1
}
finally {
    Remove-Item -LiteralPath $stdoutTmp, $stderrTmp -ErrorAction SilentlyContinue
}

Write-SchedulerLog "END run_bot.ps1 (exit code $exitCode)"
exit $exitCode
