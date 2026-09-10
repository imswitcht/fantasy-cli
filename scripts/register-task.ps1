<#
    Registers Windows Scheduled Tasks that run the autopilot before kickoff.

    Run once, from an elevated PowerShell, in the project root:

        powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1

    Add -Apply to register tasks that actually submit lineup changes. Without
    it the tasks run in dry-run mode and only write to logs\ -- strongly
    recommended for the first couple of weeks so you can see what it *would*
    have done before letting it touch a real lineup.

    Times are local. The three windows cover the three slates that matter:
      Sunday  11:30  - 45 min before the 1pm ET games (adjust for your zone)
      Sunday  15:45  - before the late afternoon games
      Sunday  18:45  - before Sunday night
      Thursday 18:45 - before Thursday night
      Monday   18:45 - before Monday night
#>
param(
    [switch]$Apply,
    [string]$TaskPrefix = "FantasyAutopilot"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "scripts\autopilot.ps1"

if (-not (Test-Path $script)) { throw "Cannot find $script" }

$applyArg = if ($Apply) { " -Apply" } else { "" }
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -NoProfile -File `"$script`"$applyArg" `
    -WorkingDirectory $root

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -RunOnlyIfNetworkAvailable

$windows = @(
    @{ Name = "Sun-Early"; Day = "Sunday";   Time = "11:30" },
    @{ Name = "Sun-Late";  Day = "Sunday";   Time = "15:45" },
    @{ Name = "Sun-Night"; Day = "Sunday";   Time = "18:45" },
    @{ Name = "Thu-Night"; Day = "Thursday"; Time = "18:45" },
    @{ Name = "Mon-Night"; Day = "Monday";   Time = "18:45" }
)

foreach ($w in $windows) {
    $name = "$TaskPrefix-$($w.Name)"
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $w.Day -At $w.Time

    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Settings $settings -Description "Fantasy football inactive-player autopilot" | Out-Null
    Write-Host "Registered $name ($($w.Day) $($w.Time), apply=$Apply)"
}

Write-Host ""
Write-Host "Done. Inspect with:  Get-ScheduledTask -TaskName '$TaskPrefix-*'"
Write-Host "Remove with:         Get-ScheduledTask -TaskName '$TaskPrefix-*' | Unregister-ScheduledTask -Confirm:`$false"
if (-not $Apply) {
    Write-Host ""
    Write-Host "These are DRY RUN tasks. Re-run with -Apply once you trust the output." -ForegroundColor Yellow
}
