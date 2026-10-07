<#
.SYNOPSIS
    Is the tracker still installed and recording? (Windows)

.DESCRIPTION
        .\scripts\check.ps1

    For troubleshooting, run from a terminal. Reads only - it changes nothing, and is
    safe to run while the game is open.

    It resolves Python itself rather than invoking a bare "python". Windows ships an App
    execution alias at %LOCALAPPDATA%\Microsoft\WindowsApps\python3.exe that is a real
    file on PATH with no Python installed, and since the installer can put a runtime in
    ~/.ptcgl-tracker/python, "python" may not be on PATH at all on a machine where the
    tracker is working perfectly. A health check that reports a failure because it could
    not find its own interpreter is worse than no health check.
#>
$ErrorActionPreference = "Stop"

# This script lives in scripts\ but works from the repo root, so every path below stays
# relative to the checkout rather than to the script.
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

# Only for Find-Python. Dot-sourcing defines functions and runs nothing, and
# Start-PtcglLog is deliberately not called: the install log is a record of installs, and
# a health check in it would only make a support log harder to read.
. (Join-Path $Root "scripts\windows-common.ps1")

try {
    # No -Download. Fetching an 11MB runtime is something the installer does because you
    # asked it to install; a check that only reads should never do it behind your back.
    $Python = Find-Python
} catch {
    Write-Host ""
    Write-Host "The health check could not start."
    Write-Host ""
    foreach ($line in ("$($_.Exception.Message)" -split "`r?`n")) { Write-Host "  $line" }
    Write-Host ""
    exit 1
}

& $Python (Join-Path $Root "scripts\healthcheck.py") @args

# A native command that fails does not raise, so the exit code is the only evidence of
# what happened. Pass it on unchanged: 0 means recording, anything else means it is not.
exit $LASTEXITCODE
