<#
.SYNOPSIS
    Run the match watcher automatically, so launching the game is all you have to do
    (Windows).

.DESCRIPTION
    Installs a per-user Scheduled Task that starts scripts\watch.py at logon and
    restarts it if it dies. The watcher reads the tracker's event log, sends matches on,
    and checks the install - it does not touch the game while it is running, so leaving it
    running costs nothing.

    Unlike the macOS watcher, this points straight at the repo checkout: Windows has no
    equivalent of ~/Documents being TCC-protected, so no deployed copy is needed. Edits to
    the Python files take effect the next time the task restarts the process; re-run this
    script only if you move the checkout.

    Also records the relauncher (~/.ptcgl-tracker/relauncher.txt) that a game update hands
    over to, so the updated game starts with the tracker already back in it.

.PARAMETER Stop
    Stop and remove the scheduled task.

.PARAMETER Quiet
    With -Stop: say nothing on the console. For the installer, which stops any watcher
    before it touches the game.
#>
param(
    [switch]$Stop,
    [switch]$Quiet
)
$ErrorActionPreference = "Stop"

# The process sweep below matches any *watch.py*, so re-running the installer replaces a
# running watcher cleanly rather than leaving a second one behind.
$TaskName = "CbrewTrackerWatcher"
$Startup = [Environment]::GetFolderPath('Startup')
$Shortcut = Join-Path $Startup "cbrew Tracker Watcher.lnk"

function Stop-RunningWatcher {
    <#
    Kill any watcher already polling. Without this, re-running the installer leaves the
    old process alive and a second one starts beside it - two watchers sending the same
    matches and each opening its own alert. The launcher may be python.exe, python3.exe or
    a versioned python3.13.exe, so match the family and confirm by command line.
    #>
    $live = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*watch.py*" }
    foreach ($p in $live) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            Write-Detail "stopped running watcher (pid $($p.ProcessId))"
        } catch {
            Write-Detail "could not stop pid $($p.ProcessId): $($_.Exception.Message)"
        }
    }
    return @($live).Count
}

function Remove-WatcherRegistrations {
    <#
    Every way a watcher can be set to start at login: this one's task and shortcut, and
    any registered by another build of this tracker under its own name. Two registrations
    would start two watchers at every login, and the other one would keep putting its own
    build back into the game.
    #>
    $tasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
        $_.TaskName -eq $TaskName -or $_.TaskName -like "PtcglTracker*Watcher"
    })
    foreach ($t in $tasks) {
        try {
            Unregister-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath -Confirm:$false -ErrorAction Stop
            if ($t.TaskName -ne $TaskName) { Write-Detail "removed another build's task ($($t.TaskName))" }
        } catch { }
    }
    foreach ($lnk in @(Get-ChildItem -Path $Startup -Filter "*Tracker*Watcher.lnk" -ErrorAction SilentlyContinue)) {
        Remove-Item $lnk.FullName -Force -ErrorAction SilentlyContinue
    }
}

# Dot-sourced before -Stop is handled: Stop-RunningWatcher narrates through Write-Detail,
# which lives in there.
. (Join-Path $PSScriptRoot "windows-common.ps1")

if ($Stop) {
    Remove-WatcherRegistrations
    Stop-RunningWatcher | Out-Null
    if (-not $Quiet) { Write-Host "Watcher stopped and removed." }
    exit 0
}

# No -Download here: install.ps1 has already resolved (or fetched) the runtime by the time
# it calls this, so the same interpreter is found again without touching the network.
$Python = Find-Python

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$WatchScript = Join-Path $Root "scripts\watch.py"
$LogDir = Join-Path $env:USERPROFILE ".ptcgl-tracker"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir "watcher.log"

# Launched through pythonw.exe, the console-less build of the interpreter, with the watcher
# writing its own log via --log.
#
# A process with no interface should not be represented by a console window at all: hidden
# or minimised is not the same as absent, and a minimised one leaves a terminal on the
# taskbar at every login. pythonw allocates no console, so there is nothing to hide. It has
# no stdout either, which is exactly why --log exists: the watcher opens the file itself, in
# UTF-8 (PowerShell 5.1's own `>>` would write UTF-16LE).
$PythonW = Join-Path (Split-Path $Python -Parent) "pythonw.exe"
if (-not (Test-Path $PythonW)) {
    # Every python.org build ships pythonw beside python, embeddable package included -
    # but if it is somehow absent, a console window is a far better outcome than a
    # watcher that does not start.
    $PythonW = $Python
    Write-Detail "no pythonw.exe beside $Python - falling back to a console window"
}

$Argument = "`"$WatchScript`" --log `"$LogFile`""

# --- the relauncher -------------------------------------------------------------------
#
# What Pokemon's updater runs at the end of a game update, instead of the game. The mod's
# UpdaterHandoffHook reads this file when the game starts its updater, and points the
# updater's --launchAppAt at the interpreter and --launchAppWithArgs at the script; once
# the new version is copied in and Play is clicked, scripts\relaunch.py puts the tracker
# back and then starts the game. The updated game starts with the tracker already in it.
#
# Two lines, interpreter then script, because the mod reads it with nothing but
# File.ReadAllLines. pythonw for the same reason as the watcher: no console to flash up as
# the updater closes. BOM-free UTF-8, since a user folder may not be ASCII.
#
# Proved runnable before it is recorded. The mod checks both files exist before handing an
# update over, but it cannot check that Python will actually run the script - this can, and
# a relauncher that fails here would make the update end with the game closed. If the check
# fails the file is removed, and updates fall back to re-attaching once the game is closed.
$RelaunchScript = Join-Path $Root "scripts\relaunch.py"
$RelauncherFile = Join-Path $LogDir "relauncher.txt"
$relaunchCheck = Invoke-Tool { & $Python $RelaunchScript --check }
if ($relaunchCheck.ExitCode -eq 0) {
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [IO.File]::WriteAllText($RelauncherFile, "$PythonW`r`n$RelaunchScript`r`n", $utf8)
    Write-Detail $relaunchCheck.Output
    Write-Detail "relauncher: $PythonW `"$RelaunchScript`""
} else {
    Remove-Item $RelauncherFile -Force -ErrorAction SilentlyContinue
    Write-Detail "the relauncher could not run here - updates will re-attach on the next game close instead"
    Write-Detail $relaunchCheck.Output
}

# For anyone restarting the watcher by hand. It starts pythonw and returns immediately, so
# it leaves nothing on screen either.
$RunnerPath = Join-Path $LogDir "run-watcher.ps1"
@"
Start-Process -FilePath "$PythonW" -ArgumentList '$Argument' -WindowStyle Hidden
"@ | Set-Content -Path $RunnerPath -Encoding utf8

# Preferred: a Scheduled Task, which also restarts the watcher if it dies. Registering
# one needs an *elevated* shell - being in the Administrators group is not enough,
# because UAC hands an unelevated session a filtered token and the task service honours
# that. Rather than demand "run as Administrator", fall back to the Startup folder,
# which needs no elevation and gets the same job done minus the auto-restart.
Remove-WatcherRegistrations
Stop-RunningWatcher | Out-Null
$viaTask = $false
try {
    $action = New-ScheduledTaskAction -Execute $PythonW `
        -Argument $Argument -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description "cbrew Tracker background service" `
        -ErrorAction Stop | Out-Null
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $viaTask = $true
} catch {
    Write-Detail "Scheduled Task needs an elevated shell here; using the Startup folder instead"
}

if (-not $viaTask) {
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($Shortcut)
    $lnk.TargetPath = $PythonW
    $lnk.Arguments = $Argument
    $lnk.WorkingDirectory = $Root
    # 1 = normal. There is no window to style: pythonw allocates no console, and 7
    # (minimised) would put a taskbar button there at every login.
    $lnk.WindowStyle = 1
    $lnk.Description = "cbrew Tracker background service"
    $lnk.Save()
    if (-not (Test-Path $Shortcut)) {
        throw ("The tracker is installed in the game, but it could not be set to start with" +
               "`nWindows. You can start it yourself with:" +
               "`n  $Python $WatchScript")
    }
    # Start it now so this session is covered too, not just the next login.
    Start-Process -FilePath $PythonW -ArgumentList $Argument -WindowStyle Hidden
}

Start-Sleep -Seconds 2
# The launcher may be python.exe, python3.exe or a versioned python3.13.exe depending
# on how Python was installed, so match the family and confirm by command line.
$running = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*watch.py*" }

Write-Step "Set it to start with Windows"
if ($viaTask) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Write-Detail "Scheduled Task (state: $($task.State)) - starts at login, restarts if it dies"
} else {
    Write-Detail "Startup folder - starts at login, no auto-restart"
    Write-Detail "run from an elevated PowerShell to get a Scheduled Task with restart instead"
    Write-Detail "shortcut: $Shortcut"
}
Write-Detail "running now: $(if ($running) { 'yes, pid ' + ($running.ProcessId -join ', ') } else { 'not detected - check the log' })"
Write-Detail "log: $LogFile"
Write-Detail "stop with: .\scripts\install-watcher-windows.ps1 -Stop"
