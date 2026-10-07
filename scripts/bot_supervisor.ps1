# bot_supervisor.ps1 - keeps bot_listener.py running forever, in a HIDDEN window.
# If the bot stops or crashes for ANY reason it is started again.
# Started by the scheduled task created with install_autostart.bat (do not run it twice - it protects itself).

$ErrorActionPreference = "Continue"
# Project root is one level above this script (which lives in scripts/)
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -Path $projectRoot

# --- only one supervisor at a time (across all Windows sessions) ---
$md5 = [Security.Cryptography.MD5]::Create()
$hash = [BitConverter]::ToString($md5.ComputeHash([Text.Encoding]::UTF8.GetBytes($projectRoot.ToLower()))).Replace("-", "")
$created = $false
$mutex = New-Object System.Threading.Mutex($true, "Global\FescoBillBotSupervisor-$hash", [ref]$created)
if (-not $created) { exit 0 }

$logsDir = Join-Path $projectRoot "logs"
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir | Out-Null }
$logFile = Join-Path $logsDir "bot_supervisor.log"
function Write-Log($text) {
    try {
        if ((Test-Path $logFile) -and ((Get-Item $logFile).Length -gt 1MB)) { Move-Item $logFile "$logFile.1" -Force }
        Add-Content -Path $logFile -Value ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $text)
    } catch { }
}

# --- which Python: the project's virtual environment first, then PATH ---
$py = $null
foreach ($candidate in @(".venv\Scripts\python.exe", "venv\Scripts\python.exe")) {
    $full = Join-Path $projectRoot $candidate
    if (Test-Path $full) { $py = $full; break }
}
if (-not $py) {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if ($found) { $py = $found.Source }
}
if (-not $py) {
    Write-Log "ERROR: Python not found (no .venv or venv folder here, and nothing on PATH)."
    exit 2
}

$botScript = Join-Path $projectRoot "src\bot_listener.py"
Write-Log "Supervisor started. Python: $py | Project root: $projectRoot"
Start-Sleep -Seconds 10   # give the network a moment after logon / boot

$delay = 10
while ($true) {
    $started = Get-Date
    Write-Log "Starting src\bot_listener.py"
    $code = -1
    try {
        $proc = Start-Process -FilePath $py -ArgumentList "`"$botScript`"" -WorkingDirectory $projectRoot `
            -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $logsDir "bot_console.log") `
            -RedirectStandardError (Join-Path $logsDir "bot_console_err.log")
        $null = $proc.Handle          # keeps the exit code readable
        $proc.WaitForExit()
        if ($null -ne $proc.ExitCode) { $code = $proc.ExitCode }
    } catch {
        Write-Log "Could not start the bot: $_"
    }

    $ran = [int]((Get-Date) - $started).TotalSeconds
    Write-Log "Bot exited with code $code after $ran seconds"

    if ($code -eq 4) {
        Write-Log "Another copy of the bot is already running - this supervisor stops."
        break
    }
    # Healthy run (2+ minutes) -> restart quickly. Crashing straight away -> back off, max 5 minutes.
    if ($ran -ge 120) { $delay = 10 } else { $delay = [Math]::Min($delay * 2, 300) }
    Write-Log "Restarting in $delay seconds"
    Start-Sleep -Seconds $delay
}
