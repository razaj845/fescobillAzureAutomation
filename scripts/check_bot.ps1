# check_bot.ps1 - answers "is the bot alive, and if not, why?" without guessing.
Set-Location -Path $PSScriptRoot
$taskName = "FESCO Bill Bot"

Write-Host "=== Scheduled task ===" -ForegroundColor Cyan
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "NOT INSTALLED. Run install_autostart.bat." -ForegroundColor Red
} else {
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    $s = $task.Settings
    Write-Host ("State             : {0}" -f $task.State)
    Write-Host ("Last run          : {0}" -f $info.LastRunTime)
    Write-Host ("Last result       : 0x{0:X}   (0 = ok, 0x41301 = running now, 0x41306 = was stopped by Windows/user)" -f $info.LastTaskResult)
    Write-Host ("Next run          : {0}" -f $info.NextRunTime)
    Write-Host ("Stop on battery   : {0}   (must be False)" -f $s.StopIfGoingOnBatteries)
    Write-Host ("Time limit        : {0}   (must be PT0S)" -f $s.ExecutionTimeLimit)
    Write-Host ("If already running: {0}" -f $s.MultipleInstances)
}

Write-Host ""
Write-Host "=== Running processes ===" -ForegroundColor Cyan
$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "bot_listener\.py|bot_supervisor\.ps1" -and $_.ProcessId -ne $PID }
if ($procs) {
    $procs | ForEach-Object { Write-Host ("PID {0}  {1}  started {2}" -f $_.ProcessId, $_.Name, $_.CreationDate) }
} else {
    Write-Host "No bot process is running." -ForegroundColor Red
}

foreach ($name in @("bot_supervisor.log", "bot.log", "bot_console_err.log")) {
    $path = Join-Path $PSScriptRoot $name
    Write-Host ""
    Write-Host "=== $name (last 12 lines) ===" -ForegroundColor Cyan
    if ((Test-Path $path) -and ((Get-Item $path).Length -gt 0)) { Get-Content $path -Tail 12 } else { Write-Host "(empty or missing)" }
}

Write-Host ""
Write-Host "=== Power ===" -ForegroundColor Cyan
if (Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue) {
    Write-Host "This PC has a battery (laptop). If it SLEEPS, the bot pauses. Run: install_autostart.bat -KeepAwake"
} else {
    Write-Host "Desktop PC (no battery)."
}
powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 2>$null | Select-String "Current AC Power Setting Index|Current DC Power Setting Index"
Write-Host "(index 0x00000000 = never sleep)"
