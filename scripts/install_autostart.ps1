# install_autostart.ps1 - creates the Windows scheduled task that keeps the bot running.
#
#   install_autostart.bat                      -> runs when you are logged in (normal setup)
#   install_autostart_background.bat           -> ALSO runs at boot with nobody logged in (run as Administrator)
#   add  -KeepAwake  to stop the PC sleeping while plugged in
#
param(
    [switch]$RunWithoutLogin,
    [switch]$KeepAwake
)
$ErrorActionPreference = "Stop"

$taskName   = "FESCO Bill Bot"
$dir        = $PSScriptRoot
$supervisor = Join-Path $dir "bot_supervisor.ps1"
if (-not (Test-Path $supervisor)) { throw "bot_supervisor.ps1 was not found next to this script." }

if ($RunWithoutLogin) {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) { throw "-RunWithoutLogin needs an Administrator window: right-click the .bat file and choose 'Run as administrator'." }
}

# Remove any older version of the task first (including one you created by hand with the same name).
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

# What to run: the supervisor, hidden, started IN the project folder.
$psArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$supervisor`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $psArgs -WorkingDirectory $dir

# When to run it: at logon (and at boot if asked), PLUS a watchdog every 5 minutes.
# The watchdog does nothing while the bot is running (MultipleInstances = IgnoreNew + the bot's own
# single-instance lock) but brings everything back if it was ever killed.
$me = "$env:USERDOMAIN\$env:USERNAME"
$triggers = @()
if ($RunWithoutLogin) { $triggers += New-ScheduledTaskTrigger -AtStartup }
$triggers += New-ScheduledTaskTrigger -AtLogOn -User $me
$triggers += New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(2)) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)

# The settings that make it "never stop": Windows' defaults would stop the task on battery power,
# after a time limit, or when the PC goes idle.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -DontStopOnIdleEnd

if ($RunWithoutLogin) {
    $cred = Get-Credential -UserName $me -Message "Your Windows password - needed so the bot can run even when nobody is logged in"
    if (-not $cred) { throw "Cancelled." }
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings `
        -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Limited -Force | Out-Null
} else {
    $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings `
        -Principal $principal -Force | Out-Null
}

# Read the settings back so you can SEE they were applied.
$task = Get-ScheduledTask -TaskName $taskName
$s = $task.Settings
Write-Host ""
Write-Host "Task '$taskName' created. Settings now in effect:"
Write-Host ("  Stop if running on battery power : {0}  (should be False)" -f $s.StopIfGoingOnBatteries)
Write-Host ("  Don't start on battery           : {0}  (should be False)" -f $s.DisallowStartIfOnBatteries)
Write-Host ("  Time limit                       : {0}  (PT0S = no limit)" -f $s.ExecutionTimeLimit)
Write-Host ("  If already running               : {0}  (should be IgnoreNew)" -f $s.MultipleInstances)
Write-Host ("  Restart if it fails              : {0} times, every {1}" -f $s.RestartCount, $s.RestartInterval)

if ($KeepAwake) {
    powercfg /change standby-timeout-ac 0 | Out-Null
    powercfg /change hibernate-timeout-ac 0 | Out-Null
    Write-Host "  Sleep/hibernate while plugged in : turned OFF"
}

Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 3
Write-Host ""
Write-Host ("Task state: {0}" -f (Get-ScheduledTask -TaskName $taskName).State)
Write-Host "Done. Run check_bot.bat any time to see whether the bot is alive."
