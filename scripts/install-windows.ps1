<#
.SYNOPSIS
    Install the PTCGL tracker into the game (Windows).

.DESCRIPTION
    The tracker loads as one of the game's own managed assemblies, registered in Unity's
    RuntimeInitializeOnLoads manifest - the same mechanism scripts/install-macos.sh uses,
    ported to the Windows layout (<game>\<Name>_Data\ instead of a signed .app bundle).
    No BepInEx, no Doorstop: nothing here touches the executable, so there is no
    re-signing step and no antivirus-triggering proxy DLL.

    Idempotent - safe to re-run. After a game update the watcher normally puts the tracker
    back on its own; re-running this is the answer when it cannot.

.PARAMETER GamePath
    The game's .exe, its "<Name>_Data" folder, or the folder it's installed into. If
    omitted, the known default install location is tried first.
#>
param(
    [string]$GamePath
)
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "windows-common.ps1")

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Build = Join-Path $Root "mod\build"
$Backup = Join-Path $Root "backup\windows\manifests"

$Python = Find-Python

$Data = Find-GameData -Hint $GamePath
if (-not $Data) {
    throw ("Could not find Pokemon TCG Live on this PC." +
           "`nIf it is installed somewhere unusual, point at it:" +
           "`n  .\scripts\install.ps1 -GamePath 'C:\path\to\the\game'")
}
Write-Step "Found the game"
Write-Detail "Game data: $Data"
$Managed = Join-Path $Data "Managed"

if (-not (Test-Path $Managed)) {
    throw "That does not look like the game folder - there is no Managed folder in $Data."
}
if (-not (Test-Path (Join-Path $Build "CbrewTracker.dll"))) {
    throw "The tracker was not built. Run Install-Windows.cmd rather than this script directly."
}

# Another build of this tracker in the game would record every match twice, and nothing
# about the game looks wrong while it does - so the installer is the one to catch it. Its
# files are locked while the game runs, so the game has to be closed for this.
$foreign = Invoke-Tool { & $Python (Join-Path $PSScriptRoot "modinstall.py") --foreign $Data }
$foreignNames = @($foreign.Output -split "`r?`n" | Where-Object { $_.Trim() })
if ($foreign.ExitCode -eq 0 -and $foreignNames.Count -gt 0) {
    Write-Step "Removing an older tracker already in the game"
    $gone = Invoke-Tool {
        & $Python (Join-Path $PSScriptRoot "modinstall.py") --remove $Data --names @foreignNames
    }
    Write-Detail $gone.Output
    if ($gone.ExitCode -ne 0) {
        throw ("An older tracker is in the game and could not be removed. Quit Pokemon TCG " +
               "Live and run this again.`n" + $gone.Output)
    }
}

Write-Detail "Backing up Unity manifests (first run only)"
New-Item -ItemType Directory -Force -Path $Backup | Out-Null
# The backup is only meaningful for the install it was taken from. Stamp it with that
# path and re-take when it does not match, otherwise a backup captured against another
# copy of the game (or a test fixture) would be restored over a real one.
$Stamp = Join-Path $Backup "source.txt"
$StaleBackup = (Test-Path $Stamp) -and ((Get-Content $Stamp -Raw).Trim() -ne $Data)
if ($StaleBackup) {
    Write-Detail "existing backup was taken from a different install - re-taking"
}
foreach ($f in "ScriptingAssemblies.json", "RuntimeInitializeOnLoads.json") {
    $dst = Join-Path $Backup $f
    if ($StaleBackup -or -not (Test-Path $dst)) {
        Copy-Item (Join-Path $Data $f) $dst -Force
    }
}
Set-Content -Path $Stamp -Value $Data -Encoding utf8

Write-Detail "Checking the assemblies are real CIL images"
# Mono rejects metadata-only reference assemblies with "File does not contain a valid
# CIL image". NuGet hands out exactly such an assembly for Harmony (Lib.Harmony.Ref), so
# verify here rather than discovering it from a game log later.
$verifyScript = @'
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(sys.argv[2]) / "ingest"))
from clrmeta import read_assembly, is_reference_assembly

build = pathlib.Path(sys.argv[1])
for name in ("CbrewTracker.dll", "0Harmony.dll"):
    path = build / name
    try:
        asm = read_assembly(str(path))
    except Exception as exc:
        sys.exit(f"    FAIL {name}: not a readable .NET assembly ({exc})")
    if is_reference_assembly(str(path)):
        sys.exit(f"    FAIL {name}: this is a reference assembly, not an implementation "
                 f"build. Mono will reject it as 'not a valid CIL image'.")
    methods = sum(len(t.methods) for t in asm.values())
    print(f"    OK {name}: {len(asm)} types, {methods} methods")
'@
$verify = Invoke-Tool { $verifyScript | & $Python - $Build $Root }
Write-Detail $verify.Output
if ($verify.ExitCode -ne 0) {
    throw ("The tracker's files did not pass their own check, so nothing was copied into " +
           "the game.`n" + $verify.Output)
}

# Keep a copy of the assemblies on this machine BEFORE touching the game.
#
# This is what makes an unattended repair possible at all. Without it the only copy of the
# tracker lives in the folder the user unzipped - which looks like leftovers and gets
# deleted - so a game update would leave nothing on the machine to restore from.
Write-Detail "Keeping a copy for future repairs"
$stashed = Invoke-Tool { & $Python (Join-Path $PSScriptRoot "modinstall.py") --stash $Build }
Write-Detail $stashed.Output
if ($stashed.ExitCode -ne 0) {
    throw ("Could not keep a repair copy of the tracker's files.`n" + $stashed.Output)
}

# Copying and registering both go through modinstall.py, which the watcher's repair path
# also calls. Two implementations of this would be two ways to produce a subtly different
# install, and the difference would show up only as hooks that quietly fail to attach.
Write-Detail "Copying assemblies into Managed\ and registering with Unity"
$installed = Invoke-Tool {
    & $Python (Join-Path $PSScriptRoot "modinstall.py") --apply $Data --source $Build
}
Write-Detail $installed.Output
if ($installed.ExitCode -ne 0) {
    throw ("The tracker's files were copied but the game did not accept them.`n" +
           $installed.Output)
}

# Record where we installed, so the watcher's health check can verify this exact copy
# rather than re-guessing the location every time it runs.
$Receipt = Join-Path $env:USERPROFILE ".ptcgl-tracker\install.json"
New-Item -ItemType Directory -Force -Path (Split-Path $Receipt) | Out-Null
@{
    game_data    = $Data
    installed_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    platform     = "windows"
} | ConvertTo-Json | Set-Content -Path $Receipt -Encoding utf8

Write-Step "Installed the tracker"
Write-Detail "receipt: $Receipt"
Write-Detail "after the game runs, proof of a working install is the 'patched ...' lines in"
Write-Detail "$env:USERPROFILE\.ptcgl-tracker\tracker.log"
