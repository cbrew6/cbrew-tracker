<#
.SYNOPSIS
    One-step install (Windows).

.DESCRIPTION
        .\scripts\install.ps1

    Normally reached by double-clicking Install-Windows.cmd; run it directly only to pass
    -GamePath. Finds the game, installs the tracker into it, and sets the watcher to run
    at login via a Scheduled Task. After this, launch the game normally - nothing opens
    and nothing is displayed. Matches record and send in the background.

    A game update removes the tracker; the watcher puts it back by itself. Re-run this
    if it ever says it could not.

.PARAMETER GamePath
    The game's .exe, its "<Name>_Data" folder, or the folder it's installed into. If
    omitted, the known default install location is tried first.

.PARAMETER Detail
    Show every step on the console. Without it you get four plain lines; the detail is
    written to ~/.ptcgl-tracker/install.log either way.
#>
param(
    [string]$GamePath,
    [switch]$Detail
)
$ErrorActionPreference = "Stop"

# This script lives in scripts\ but works from the repo root, so every path below stays
# relative to the checkout rather than to the script.
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

. (Join-Path $Root "scripts\windows-common.ps1")
Start-PtcglLog -Detail:$Detail

Write-Host "cbrew Tracker - installing"
Write-Host ""

try {
    # --- prerequisites --------------------------------------------------------------
    # -Download is passed here and nowhere else: the installer is the one place where
    # fetching a runtime is what the person asked for. The other scripts use whatever is
    # already there, and say to run this when there is nothing.
    $Python = Find-Python -Download
    Write-Detail "Python: $Python"

    # --- the mod assemblies ---------------------------------------------------------
    # Prefer a local build if the toolchain is present, otherwise use the shipped copy,
    # so installing needs no .NET SDK.
    $Build = Join-Path $Root "mod\build"
    New-Item -ItemType Directory -Force -Path $Build | Out-Null

    $dotnet = Get-Command dotnet -ErrorAction SilentlyContinue
    $gameLibs = Join-Path $Root "mod\libs\game"
    $haveGameLibs = (Test-Path $gameLibs) -and (Get-ChildItem $gameLibs -ErrorAction SilentlyContinue)

    if ($dotnet -and $haveGameLibs) {
        Write-Detail "Building from source"
        Push-Location (Join-Path $Root "mod\CbrewTracker")
        try {
            $out = & dotnet build -c Release -o ..\build 2>&1
            if ($LASTEXITCODE -ne 0) {
                Write-Detail ($out -join "`n")
                throw "The tracker could not be built from source. See the log for what dotnet said."
            }
        } finally {
            Pop-Location
        }
        Write-Detail "built"
    } elseif ((Test-Path (Join-Path $Root "mod\dist\CbrewTracker.dll")) `
            -and (Test-Path (Join-Path $Root "mod\dist\0Harmony.dll"))) {
        Write-Detail "Using the shipped build (no .NET toolchain needed)"
        Copy-Item (Join-Path $Root "mod\dist\CbrewTracker.dll") $Build -Force
        Copy-Item (Join-Path $Root "mod\dist\0Harmony.dll") $Build -Force
    } else {
        throw ("This copy of the tracker is incomplete - mod\dist\ has no assemblies in it." +
               "`nDownload it again from the releases page.")
    }

    # --- into the game --------------------------------------------------------------
    # Any running watcher is stopped first, this build's or another's. One left running
    # while the game's files change could put back exactly what this install replaces;
    # the watcher step below starts a fresh one.
    & (Join-Path $Root "scripts\install-watcher-windows.ps1") -Stop -Quiet
    & (Join-Path $Root "scripts\install-windows.ps1") -GamePath $GamePath

    # --- the watcher ----------------------------------------------------------------
    & (Join-Path $Root "scripts\install-watcher-windows.ps1")

    # --- send anything already recorded --------------------------------------------
    # A first submission proves the whole chain works before the player ever launches the
    # game, and on a machine that already has history it backfills the lot.
    #
    # A failure here is cosmetic and must never fail the install. The tracker is in place
    # by this point and the watcher retries on its own - failing to send is never failing
    # to record.
    try {
        # A native command that fails does not raise, so check the exit code rather than
        # trusting the absence of an exception.
        Write-Detail (& $Python (Join-Path $Root "ingest\ingest.py") 2>&1 | Out-String)
        if ($LASTEXITCODE -ne 0) { throw "ingest exited $LASTEXITCODE" }
        Write-Detail (& $Python (Join-Path $Root "analysis\submit.py") 2>&1 | Out-String)
        if ($LASTEXITCODE -ne 0) { throw "submit exited $LASTEXITCODE" }
        Write-Step "Sent what you had recorded"
    } catch {
        Write-Detail "nothing sent now: $($_.Exception.Message)"
        Write-Step "Your matches will send after the next game"
    }

    Write-Host ""
    Write-Host "Done. Launch Pokemon TCG Live and play."
    Write-Host "Nothing to open - this client records and sends in the background."
    Write-Log "OK    install finished"
} catch {
    Write-Fail $_.Exception.Message
    exit 1
}
