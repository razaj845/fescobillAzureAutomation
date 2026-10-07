#requires -version 5.1

# ================================================================
# FESCO BILL WORKER
# PORTABLE PRODUCTION INSTALLER
# ================================================================
#
# Folder:
#   <any location>\fescobill\worker\
#
# Required files:
#   worker.py
#   worker.env
#   requirements.worker.txt
#   install_worker.ps1
#
# The script automatically:
#
#   1. Finds its own location
#   2. Finds Python 3.10+
#   3. Creates a local virtual environment
#   4. Installs worker dependencies
#   5. Creates the worker launcher
#   6. Creates a Windows Scheduled Task
#   7. Runs the task as SYSTEM
#   8. Starts at Windows boot
#   9. Works without user login
#  10. Works on battery
#  11. Does not stop when switching to battery
#  12. Has unlimited execution time
#  13. Automatically restarts the task
#  14. Prevents duplicate worker instances
#  15. Starts the worker immediately
#  16. Verifies the configuration
#
# IMPORTANT:
# Run this file as Administrator.
# The script can request Administrator access automatically.
#
# ================================================================

$ErrorActionPreference = "Stop"

# ================================================================
# CONFIGURATION
# ================================================================

$TaskName = "FESCO Bill Worker"

# This is the folder where install_worker.ps1 is located.
$WorkerDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

# Project directory = one level above worker.
$ProjectDir = Split-Path -Parent $WorkerDir

$WorkerScript     = Join-Path $WorkerDir "worker.py"
$WorkerEnv        = Join-Path $WorkerDir "worker.env"
$RequirementsFile = Join-Path $WorkerDir "requirements.worker.txt"
$Launcher         = Join-Path $WorkerDir "worker_launcher.bat"

$VenvDir          = Join-Path $ProjectDir ".venv"
$VenvPython       = Join-Path $VenvDir "Scripts\python.exe"

$LogDir           = Join-Path $ProjectDir "logs"
$LauncherLog      = Join-Path $LogDir "worker_launcher.log"
$InstallLog       = Join-Path $LogDir "install_worker.log"

# ================================================================
# FUNCTIONS
# ================================================================

function Write-Info {
    param(
        [string]$Message
    )

    Write-Host "[INFO]  $Message" -ForegroundColor Cyan
}

function Write-OK {
    param(
        [string]$Message
    )

    Write-Host "[OK]    $Message" -ForegroundColor Green
}

function Write-Warn {
    param(
        [string]$Message
    )

    Write-Host "[WARN]  $Message" -ForegroundColor Yellow
}

function Write-Fail {
    param(
        [string]$Message
    )

    Write-Host "[ERROR] $Message" -ForegroundColor Red
}

function Fail {
    param(
        [string]$Message
    )

    Write-Fail $Message

    Write-Host ""
    Write-Host "Installation stopped." -ForegroundColor Red
    Write-Host ""

    exit 1
}

function Test-IsAdministrator {

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()

    $principal = New-Object `
        Security.Principal.WindowsPrincipal($identity)

    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Find-Python {

    # ------------------------------------------------------------
    # 1. Python Launcher (py.exe)
    # ------------------------------------------------------------

    $pyCommand = Get-Command `
        "py.exe" `
        -ErrorAction SilentlyContinue

    if ($pyCommand) {

        try {

            $result = & $pyCommand.Source `
                -3 `
                -c "import sys; print(sys.executable)" `
                2>$null

            if ($LASTEXITCODE -eq 0 -and $result) {

                $path = ($result | Select-Object -Last 1).Trim()

                if ((Test-Path $path) -and
                    ($path -notmatch "\\WindowsApps\\")) {

                    return $path
                }
            }
        }
        catch {
        }
    }

    # ------------------------------------------------------------
    # 2. python.exe in PATH
    # ------------------------------------------------------------

    $pythonCommand = Get-Command `
        "python.exe" `
        -ErrorAction SilentlyContinue

    if ($pythonCommand) {

        try {

            $result = & $pythonCommand.Source `
                -c "import sys; print(sys.executable)" `
                2>$null

            if ($LASTEXITCODE -eq 0 -and $result) {

                $path = ($result | Select-Object -Last 1).Trim()

                if ((Test-Path $path) -and
                    ($path -notmatch "\\WindowsApps\\")) {

                    return $path
                }
            }
        }
        catch {
        }
    }

    # ------------------------------------------------------------
    # 3. Search LocalAppData Python installations
    # ------------------------------------------------------------

    if ($env:LOCALAPPDATA) {

        $pythonRoot = Join-Path `
            $env:LOCALAPPDATA `
            "Programs\Python"

        if (Test-Path $pythonRoot) {

            $folders = Get-ChildItem `
                -Path $pythonRoot `
                -Directory `
                -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending

            foreach ($folder in $folders) {

                $candidate = Join-Path `
                    $folder.FullName `
                    "python.exe"

                if (Test-Path $candidate) {

                    return $candidate
                }
            }
        }
    }

    # ------------------------------------------------------------
    # 4. Common system locations
    # ------------------------------------------------------------

    $commonLocations = @(
        "C:\Python313\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe",
        "C:\Python310\python.exe"
    )

    foreach ($candidate in $commonLocations) {

        if (Test-Path $candidate) {

            return $candidate
        }
    }

    return $null
}

# ================================================================
# HEADER
# ================================================================

Write-Host ""
Write-Host "===============================================================" `
    -ForegroundColor Green

Write-Host " FESCO BILL WORKER - PRODUCTION INSTALLER" `
    -ForegroundColor Green

Write-Host "===============================================================" `
    -ForegroundColor Green

Write-Host ""

Write-Host "Worker folder:" -ForegroundColor Gray
Write-Host "  $WorkerDir"

Write-Host ""

Write-Host "Project folder:" -ForegroundColor Gray
Write-Host "  $ProjectDir"

Write-Host ""

# ================================================================
# ADMINISTRATOR ELEVATION
# ================================================================

if (-not (Test-IsAdministrator)) {

    Write-Info "Administrator privileges are required."
    Write-Info "Requesting Administrator access..."

    try {

        $arguments = @(
            "-NoProfile"
            "-ExecutionPolicy"
            "Bypass"
            "-File"
            "`"$($MyInvocation.MyCommand.Definition)`""
        )

        Start-Process `
            -FilePath "powershell.exe" `
            -ArgumentList $arguments `
            -Verb RunAs

        exit 0
    }
    catch {

        Fail "Could not request Administrator privileges."
    }
}

Write-OK "Running as Administrator."

# ================================================================
# CREATE LOG DIRECTORY
# ================================================================

Write-Info "Preparing log directory..."

if (-not (Test-Path $LogDir)) {

    New-Item `
        -ItemType Directory `
        -Path $LogDir `
        -Force | Out-Null
}

Write-OK "Log directory ready."

# ================================================================
# INSTALL LOG
# ================================================================

"===============================================================" |
    Out-File `
        -FilePath $InstallLog `
        -Encoding UTF8

"FESCO installer started: $(Get-Date)" |
    Out-File `
        -FilePath $InstallLog `
        -Append `
        -Encoding UTF8

"WorkerDir: $WorkerDir" |
    Out-File `
        -FilePath $InstallLog `
        -Append `
        -Encoding UTF8

"ProjectDir: $ProjectDir" |
    Out-File `
        -FilePath $InstallLog `
        -Append `
        -Encoding UTF8

# ================================================================
# VERIFY REQUIRED FILES
# ================================================================

Write-Info "Checking required files..."

if (-not (Test-Path $WorkerScript)) {

    Fail "worker.py was not found:`n$WorkerScript"
}

Write-OK "worker.py found."

if (-not (Test-Path $WorkerEnv)) {

    Fail "worker.env was not found:`n$WorkerEnv"
}

Write-OK "worker.env found."

if (-not (Test-Path $RequirementsFile)) {

    Fail "requirements.worker.txt was not found:`n$RequirementsFile"
}

Write-OK "requirements.worker.txt found."

# ================================================================
# FIND PYTHON
# ================================================================

Write-Info "Detecting Python..."

$SystemPython = Find-Python

if (-not $SystemPython) {

    Fail @"
Python 3 was not found.

Install Python 3.10 or newer and run the installer again.
"@
}

Write-OK "Python found:"
Write-Host "        $SystemPython"

# ================================================================
# CHECK PYTHON VERSION
# ================================================================

Write-Info "Checking Python version..."

$versionOutput = & $SystemPython --version 2>&1

if ($LASTEXITCODE -ne 0) {

    Fail "Python could not be executed."
}

Write-OK "$versionOutput"

$versionNumber = & $SystemPython -c `
    "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"

if (-not $versionNumber) {

    Fail "Could not determine Python version."
}

$versionParts = $versionNumber.Trim().Split(".")

$major = [int]$versionParts[0]
$minor = [int]$versionParts[1]

if (($major -ne 3) -or ($minor -lt 10)) {

    Fail "Python 3.10 or newer is required. Detected: $versionNumber"
}

Write-OK "Python version is supported."

# ================================================================
# CREATE VIRTUAL ENVIRONMENT
# ================================================================

Write-Info "Checking Python virtual environment..."

if (Test-Path $VenvPython) {

    Write-OK "Existing virtual environment found."

}
else {

    if (Test-Path $VenvDir) {

        Write-Warn "Incomplete virtual environment found."
        Write-Info "Removing incomplete .venv..."

        Remove-Item `
            -Path $VenvDir `
            -Recurse `
            -Force
    }

    Write-Info "Creating virtual environment..."

    & $SystemPython -m venv $VenvDir

    if ($LASTEXITCODE -ne 0) {

        Fail "Failed to create the Python virtual environment."
    }

    Write-OK "Virtual environment created."
}

if (-not (Test-Path $VenvPython)) {

    Fail "Virtual environment Python was not created."
}

Write-OK "Virtual environment Python:"
Write-Host "        $VenvPython"

# ================================================================
# UPDATE PIP
# ================================================================

Write-Info "Updating pip..."

& $VenvPython `
    -m pip `
    install `
    --disable-pip-version-check `
    --upgrade `
    pip

if ($LASTEXITCODE -ne 0) {

    Fail "pip upgrade failed."
}

Write-OK "pip is ready."

# ================================================================
# INSTALL DEPENDENCIES
# ================================================================

Write-Info "Installing worker dependencies..."

& $VenvPython `
    -m pip `
    install `
    --disable-pip-version-check `
    -r $RequirementsFile

if ($LASTEXITCODE -ne 0) {

    Fail "Worker dependency installation failed."
}

Write-OK "Worker dependencies installed."

# ================================================================
# COMPILE / TEST WORKER
# ================================================================

Write-Info "Testing worker.py..."

Push-Location $ProjectDir

try {

    & $VenvPython `
        -m py_compile `
        $WorkerScript

    if ($LASTEXITCODE -ne 0) {

        Fail "worker.py failed Python compilation."
    }

}
finally {

    Pop-Location
}

Write-OK "worker.py compiled successfully."

# ================================================================
# CREATE LAUNCHER
# ================================================================

Write-Info "Creating worker launcher..."

$launcherContent = @"
@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM FESCO BILL WORKER
REM AUTO-RESTART LAUNCHER
REM ============================================================

set "PROJECT_DIR=$ProjectDir"
set "WORKER_DIR=$WorkerDir"
set "WORKER_SCRIPT=$WorkerScript"
set "PYTHON_EXE=$VenvPython"
set "LOG_DIR=$LogDir"
set "LAUNCHER_LOG=$LauncherLog"

if not exist "%LOG_DIR%" (
    mkdir "%LOG_DIR%" >nul 2>&1
)

cd /d "%PROJECT_DIR%"

:START_WORKER

echo [%date% %time%] FESCO worker launcher starting... >> "%LAUNCHER_LOG%"

if not exist "%PYTHON_EXE%" (
    echo [%date% %time%] ERROR: Python executable not found: %PYTHON_EXE% >> "%LAUNCHER_LOG%"
    timeout /t 60 /nobreak >nul
    goto START_WORKER
)

if not exist "%WORKER_SCRIPT%" (
    echo [%date% %time%] ERROR: worker.py not found: %WORKER_SCRIPT% >> "%LAUNCHER_LOG%"
    timeout /t 60 /nobreak >nul
    goto START_WORKER
)

set "PYTHONPATH=%PROJECT_DIR%;%WORKER_DIR%;%PYTHONPATH%"

echo [%date% %time%] Starting worker.py... >> "%LAUNCHER_LOG%"

"%PYTHON_EXE%" -u "%WORKER_SCRIPT%" >> "%LAUNCHER_LOG%" 2>&1

set "EXIT_CODE=%ERRORLEVEL%"

echo [%date% %time%] worker.py exited with code %EXIT_CODE%. >> "%LAUNCHER_LOG%"
echo [%date% %time%] Restarting in 5 seconds... >> "%LAUNCHER_LOG%"

timeout /t 5 /nobreak >nul

goto START_WORKER
"@

Set-Content `
    -Path $Launcher `
    -Value $launcherContent `
    -Encoding ASCII

Write-OK "worker_launcher.bat created."

# ================================================================
# REMOVE EXISTING TASK
# ================================================================

Write-Info "Checking for existing Scheduled Task..."

$existingTask = Get-ScheduledTask `
    -TaskName $TaskName `
    -ErrorAction SilentlyContinue

if ($existingTask) {

    Write-Warn "Existing '$TaskName' task found."
    Write-Info "Stopping existing task..."

    Stop-ScheduledTask `
        -TaskName $TaskName `
        -ErrorAction SilentlyContinue

    Start-Sleep -Seconds 2

    Write-Info "Removing existing task..."

    Unregister-ScheduledTask `
        -TaskName $TaskName `
        -Confirm:$false

    Write-OK "Existing task removed."

}
else {

    Write-OK "No previous task found."
}

# ================================================================
# CREATE TASK ACTION
# ================================================================

Write-Info "Creating Scheduled Task action..."

# IMPORTANT:
# New-ScheduledTaskAction uses -Argument.
# Start-Process uses -ArgumentList.
#
# The worker launcher is a BAT file, therefore cmd.exe
# is used explicitly.

$action = New-ScheduledTaskAction `
    -Execute "$env:SystemRoot\System32\cmd.exe" `
    -Argument "/c `"$Launcher`"" `
    -WorkingDirectory $WorkerDir

Write-OK "Task action created."

# ================================================================
# CREATE STARTUP TRIGGER
# ================================================================

Write-Info "Creating Windows startup trigger..."

$trigger = New-ScheduledTaskTrigger `
    -AtStartup

Write-OK "Startup trigger created."

# ================================================================
# CREATE SYSTEM PRINCIPAL
# ================================================================

Write-Info "Configuring SYSTEM account..."

$principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest

Write-OK "SYSTEM account configured."

# ================================================================
# CREATE RELIABILITY SETTINGS
# ================================================================

Write-Info "Configuring reliability settings..."

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Write-OK "Reliability settings created."

# ================================================================
# REGISTER TASK
# ================================================================

Write-Info "Registering Scheduled Task..."

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "FESCO Bill Worker - starts at Windows startup and automatically restarts." `
    -Force | Out-Null

Write-OK "Scheduled Task created."

# ================================================================
# APPLY SETTINGS AGAIN
# ================================================================

Write-Info "Applying final task settings..."

Set-ScheduledTask `
    -TaskName $TaskName `
    -Settings $settings | Out-Null

Write-OK "Final task settings applied."

# ================================================================
# ENABLE TASK
# ================================================================

Write-Info "Enabling Scheduled Task..."

Enable-ScheduledTask `
    -TaskName $TaskName | Out-Null

Write-OK "Scheduled Task enabled."

# ================================================================
# START WORKER NOW
# ================================================================

Write-Info "Starting worker now..."

Start-ScheduledTask `
    -TaskName $TaskName

Start-Sleep -Seconds 5

Write-OK "Scheduled Task start requested."

# ================================================================
# READ TASK INFORMATION
# ================================================================

Write-Info "Reading task configuration..."

$task = Get-ScheduledTask `
    -TaskName $TaskName

$taskInfo = Get-ScheduledTaskInfo `
    -TaskName $TaskName

$s = $task.Settings

# ================================================================
# DISPLAY CONFIGURATION
# ================================================================

Write-Host ""
Write-Host "==============================================================="
Write-Host " SCHEDULED TASK CONFIGURATION"
Write-Host "==============================================================="

[PSCustomObject]@{

    TaskName           = $task.TaskName

    State              = $task.State

    RunAs              = $task.Principal.UserId

    RunLevel           = $task.Principal.RunLevel

    StartTrigger       = "At Windows Startup"

    StartOnBattery     = (-not $s.DisallowStartIfOnBatteries)

    StopOnBattery      = $s.StopIfGoingOnBatteries

    ExecutionLimit     = $s.ExecutionTimeLimit

    RestartCount       = $s.RestartCount

    RestartInterval    = $s.RestartInterval

    StartWhenAvailable = $s.StartWhenAvailable

    MultipleInstances  = $s.MultipleInstances

    LastRunTime        = $taskInfo.LastRunTime

    LastTaskResult     = $taskInfo.LastTaskResult

} | Format-List

# ================================================================
# VALIDATION
# ================================================================

Write-Host ""
Write-Host "==============================================================="
Write-Host " VALIDATION"
Write-Host "==============================================================="

$failed = $false

# ---------------------------------------------------------------
# SYSTEM
# ---------------------------------------------------------------

if ($task.Principal.UserId -eq "SYSTEM") {

    Write-OK "Run as SYSTEM."

}
else {

    Write-Fail "Task is not running as SYSTEM."
    $failed = $true
}

# ---------------------------------------------------------------
# BATTERY START
# ---------------------------------------------------------------

if (-not $s.DisallowStartIfOnBatteries) {

    Write-OK "Start on battery = ENABLED."

}
else {

    Write-Fail "Start on battery = DISABLED."
    $failed = $true
}

# ---------------------------------------------------------------
# BATTERY STOP
# ---------------------------------------------------------------

if (-not $s.StopIfGoingOnBatteries) {

    Write-OK "Stop on battery = DISABLED."

}
else {

    Write-Fail "Task will stop when switching to battery."
    $failed = $true
}

# ---------------------------------------------------------------
# EXECUTION LIMIT
# ---------------------------------------------------------------

if ($s.ExecutionTimeLimit -eq [TimeSpan]::Zero) {

    Write-OK "Execution time limit = UNLIMITED."

}
else {

    Write-Fail "Execution time limit is not unlimited."
    $failed = $true
}

# ---------------------------------------------------------------
# AUTOMATIC RESTART
# ---------------------------------------------------------------

if ($s.RestartCount -gt 0) {

    Write-OK "Task Scheduler automatic restart = ENABLED."

}
else {

    Write-Fail "Task Scheduler automatic restart = DISABLED."
    $failed = $true
}

# ---------------------------------------------------------------
# START WHEN AVAILABLE
# ---------------------------------------------------------------

if ($s.StartWhenAvailable) {

    Write-OK "StartWhenAvailable = ENABLED."

}
else {

    Write-Fail "StartWhenAvailable = DISABLED."
    $failed = $true
}

# ================================================================
# WORKER LOG CHECK
# ================================================================

Write-Host ""
Write-Host "==============================================================="
Write-Host " WORKER STARTUP LOG"
Write-Host "==============================================================="

Start-Sleep -Seconds 5

if (Test-Path $LauncherLog) {

    Write-OK "Launcher log created."

    Write-Host ""

    Get-Content `
        $LauncherLog `
        -Tail 20

}
else {

    Write-Warn "Launcher log is not available yet."
    Write-Warn "The worker may still be starting."
}

# ================================================================
# FINAL RESULT
# ================================================================

Write-Host ""
Write-Host "==============================================================="

if ($failed) {

    Write-Host " INSTALLATION COMPLETED WITH ERRORS" `
        -ForegroundColor Red

}
else {

    Write-Host " INSTALLATION SUCCESSFUL" `
        -ForegroundColor Green
}

Write-Host "==============================================================="
Write-Host ""

Write-Host "Worker directory:" -ForegroundColor Cyan
Write-Host "  $WorkerDir"

Write-Host ""

Write-Host "Virtual environment:" -ForegroundColor Cyan
Write-Host "  $VenvDir"

Write-Host ""

Write-Host "Scheduled Task:" -ForegroundColor Cyan
Write-Host "  $TaskName"

Write-Host ""

Write-Host "Worker launcher:" -ForegroundColor Cyan
Write-Host "  $Launcher"

Write-Host ""

Write-Host "The worker is configured to:" -ForegroundColor White

Write-Host "  [OK] Start when Windows starts"
Write-Host "  [OK] Run without user login"
Write-Host "  [OK] Run as SYSTEM"
Write-Host "  [OK] Use highest privileges"
Write-Host "  [OK] Start on battery"
Write-Host "  [OK] Continue on battery"
Write-Host "  [OK] Run indefinitely"
Write-Host "  [OK] Restart automatically"
Write-Host "  [OK] Prevent duplicate instances"

Write-Host ""

Write-Host "IMPORTANT:"
Write-Host "Your worker.env controls STARTUP_DELAY."
Write-Host "Your current configuration uses a 120-second startup delay."

Write-Host ""

Write-Host "Reboot test:"
Write-Host "Restart Windows and do not manually start worker.py."
Write-Host "The Scheduled Task should start the launcher automatically."

Write-Host ""

Write-Host "Check launcher log with:"
Write-Host ""
Write-Host "Get-Content `"$LauncherLog`" -Tail 30" `
    -ForegroundColor Yellow

Write-Host ""

if ($failed) {

    exit 2
}

exit 0

