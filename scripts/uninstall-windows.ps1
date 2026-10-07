<#
.SYNOPSIS
    Remove the tracker from the game and stop the watcher (Windows).

.DESCRIPTION
    Your match data in ~/.ptcgl-tracker is untouched. Only the tracker itself is removed.
    Reinstalling the game also takes it out of the game - nothing the tracker records lives
    inside the install folder.

.PARAMETER GamePath
    The game's .exe, its "<Name>_Data" folder, or the folder it's installed into. If
    omitted, the install receipt and then the known default install location are tried.
#>
param(
    [string]$GamePath
)
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "windows-common.ps1")

$Python = Find-Python
$Modinstall = Join-Path $PSScriptRoot "modinstall.py"
$DataDir = Join-Path $env:USERPROFILE ".ptcgl-tracker"

Write-Output "==> Stopping the watcher"
& (Join-Path $PSScriptRoot "install-watcher-windows.ps1") -Stop

Write-Output "==> Removing the tracker from the game"
$Data = $null
if (-not $GamePath) {
    $Receipt = Join-Path $DataDir "install.json"
    if (Test-Path $Receipt) {
        try {
            $Data = (Get-Content $Receipt -Raw | ConvertFrom-Json).game_data
        } catch { }
    }
    if ($Data -and -not (Test-Path $Data)) { $Data = $null }
}
if (-not $Data) { $Data = Find-GameData -Hint $GamePath }

if (-not $Data) {
    Write-Output "    the game was not found - nothing to remove from it"
    Write-Output "    if it is installed somewhere unusual, pass -GamePath 'C:\path\to\game'"
} else {
    # Surgical removal through the same code the installer uses: delete our assemblies and
    # exactly the manifest entries the install appended, leaving everything else as it was.
    # Any other build of this tracker found in the game goes with it.
    $names = @("CbrewTracker", "0Harmony")
    $foreign = Invoke-Tool { & $Python $Modinstall --foreign $Data }
    if ($foreign.ExitCode -eq 0 -and $foreign.Output) {
        $names += ($foreign.Output -split "`r?`n" | Where-Object { $_.Trim() })
    }
    $removed = Invoke-Tool { & $Python $Modinstall --remove $Data --names @names }
    Write-Output $removed.Output
    if ($removed.ExitCode -ne 0) {
        throw ("Could not remove the tracker from the game. Is Pokemon TCG Live running?" +
               " Quit it and run this again.")
    }
}

# Take away what a repair would need, so nothing can put the tracker back. Removing the
# receipt makes healthcheck.check() report "no install receipt", which repairable()
# deliberately refuses to act on; the stash goes too, so there is nothing to reinstall from.
# The relauncher's record goes with them: without the mod nothing hands an update to it.
#
# Match data - events.jsonl, the database, the logs - is untouched.
Write-Output "==> Removing the install receipt and repair copy"
foreach ($leftover in @(
    (Join-Path $DataDir "install.json"),
    (Join-Path $DataDir "mod"),
    (Join-Path $DataDir "relauncher.txt"),
    (Join-Path $DataDir "handoff.json"),
    (Join-Path $DataDir "tracker-alert.html")
)) {
    if (Test-Path $leftover) {
        Remove-Item $leftover -Recurse -Force
        Write-Output "    removed $(Split-Path $leftover -Leaf)"
    }
}

Write-Output ""
Write-Output "Done. Your match data in $DataDir is untouched."
if (Test-Path (Join-Path $DataDir "python")) {
    Write-Output "The Python the installer set up is still in $DataDir\python;"
    Write-Output "delete that folder if you want it gone. Nothing else on this PC was changed."
}
