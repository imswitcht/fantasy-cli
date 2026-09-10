<#
    Runs the conservative autopilot and appends output to a log.

    Register it with Windows Task Scheduler using register-task.ps1, or run it
    by hand to check it works:

        powershell -ExecutionPolicy Bypass -File scripts\autopilot.ps1

    Set -Apply to actually submit changes. Without it this is a dry run, which
    is how you should run it for the first week or two.
#>
param(
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ("autopilot-{0}.log" -f (Get-Date -Format "yyyy-MM"))

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

$args = @("ff.py", "autopilot")
if ($Apply) { $args += "--apply" }

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') (apply=$Apply) ===" | Out-File -Append $log
try {
    & $python @args 2>&1 | Tee-Object -Append -FilePath $log
    if ($LASTEXITCODE -ne 0) {
        "EXIT CODE $LASTEXITCODE" | Out-File -Append $log
    }
} catch {
    "FAILED: $_" | Out-File -Append $log
    exit 1
}
