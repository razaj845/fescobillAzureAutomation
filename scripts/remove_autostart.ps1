# remove_autostart.ps1 - stops the bot completely and removes the scheduled task.
$taskName = "FESCO Bill Bot"
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

Get-CimInstance Win32_Process |
    Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match "bot_listener\.py|bot_supervisor\.ps1" } |
    ForEach-Object {
        Write-Host "Stopping process $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Write-Host "The bot is stopped and the scheduled task is removed. Run install_autostart.bat to set it up again."
