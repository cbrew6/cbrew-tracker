# Shared helpers for the Windows install/uninstall/check scripts. Dot-source this file;
# it defines functions only and does not run anything on its own.
#
# Keep this file pure ASCII. PowerShell 5.1 guesses the encoding of an unmarked script,
# so an accented character here can break the whole file on someone else's machine.

# --- what the person installing actually sees ---------------------------------------
#
# About four lines a player understands on the console. Everything else - "Backing up
# Unity manifests", "Checking the assemblies are real CIL images" - means nothing to a
# player, but it is what makes a failure diagnosable. So the detail is not deleted, it is
# demoted: it always goes to ~/.ptcgl-tracker/install.log, and to the console as well when
# -Detail is passed. When someone reports a problem, ask for that file rather than a
# screenshot of a console.

function Get-PtcglLogPath {
    Join-Path $env:USERPROFILE ".ptcgl-tracker\install.log"
}

function Write-Log {
    <#
    Append one line as UTF-8 with no BOM.

    Not Add-Content -Encoding utf8: PowerShell 5.1 writes a BOM when it creates the file,
    and a BOM in a file some other tool later reads is a trap: Python's json.load rejects
    it. The .NET call takes an explicit encoding and writes exactly the bytes asked for.

    Never fails an install because the log could not be written.
    #>
    param([string]$Text)
    try {
        $utf8 = New-Object System.Text.UTF8Encoding $false
        [IO.File]::AppendAllText((Get-PtcglLogPath), "$Text`r`n", $utf8)
    } catch { }
}

function Start-PtcglLog {
    <#
    Opens the install log and records whether detail should also go to the console.

    The child scripts dot-source this file too, so state travels in the environment
    rather than in a variable: env vars are visible to a script invoked with & in the
    same process, and the run header is written once per process rather than once per
    script.
    #>
    param([switch]$Detail)
    if ($Detail) { $env:PTCGL_INSTALL_DETAIL = "1" }
    if ($env:PTCGL_INSTALL_LOG_OPEN) { return }
    $env:PTCGL_INSTALL_LOG_OPEN = "1"

    $path = Get-PtcglLogPath
    try {
        New-Item -ItemType Directory -Force -Path (Split-Path $path) | Out-Null
        # A support log nobody prunes still should not grow without limit.
        if ((Test-Path $path) -and ((Get-Item $path).Length -gt 262144)) {
            Remove-Item $path -Force -ErrorAction SilentlyContinue
        }
        Write-Log ""
        Write-Log "=== $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) install run"
    } catch { }
}

# All three of these write with Write-Host rather than Write-Output, and that is not a
# style choice. In PowerShell a function's *output stream* is its return value, so a
# helper that narrates with Write-Output silently glues its own messages onto whatever
# the caller was trying to return - Find-Python, which calls Write-Step while fetching a
# runtime, would come back as an array of log lines with the interpreter path on the end.
# These messages are for a person to read and are never data, so they belong on the host,
# not in the pipeline - which also makes them safe to call from inside any function.

function Write-Step {
    <# One line a player understands. There should be about four of these in a run. #>
    param([string]$Text)
    Write-Host "  $Text"
    Write-Log "STEP  $Text"
}

function Write-Detail {
    <# Everything else: always logged, shown only with -Detail. #>
    param([string]$Text)
    foreach ($line in ("$Text" -split "`r?`n")) {
        if ($line.Trim().Length -eq 0) { continue }
        if ($env:PTCGL_INSTALL_DETAIL) { Write-Host "      $line" }
        Write-Log "      $line"
    }
}

function Invoke-Tool {
    <#
    .SYNOPSIS
        Run a native command and return ALL of its output, as plain strings.

    .DESCRIPTION
        `& $exe ... 2>&1` looks like it captures stderr. In PowerShell 5.1 it does something
        subtly different: each stderr line comes back wrapped in an ErrorRecord, and with
        $ErrorActionPreference = "Stop" the FIRST of them raises a terminating
        NativeCommandError. The script then dies at the redirect having captured one line,
        so every multi-line failure reports only its first line and throws away the rest.

        A missing Python module would reach the user as the single useless line
        "Traceback (most recent call last):" - the ModuleNotFoundError underneath it, which
        names the problem outright, discarded before anyone could read it.

        So stderr is collected with the preference relaxed, and each record flattened to a
        string. The caller checks .ExitCode and logs .Output, and a failing tool explains
        itself.
    #>
    param([Parameter(Mandatory = $true)][scriptblock]$Command)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $lines = & $Command 2>&1 | ForEach-Object { $_.ToString() }
        return [pscustomobject]@{
            Output   = ($lines -join [Environment]::NewLine)
            ExitCode = $LASTEXITCODE
        }
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Write-Fail {
    <# The only failure message. Plain words, then where to look. #>
    param([string]$Text)
    Write-Host ""
    Write-Host "Install did not finish."
    Write-Host ""
    foreach ($line in ("$Text" -split "`r?`n")) { Write-Host "  $line" }
    Write-Host ""
    Write-Host "  Your game was not changed."
    Write-Host "  Details: $(Get-PtcglLogPath)"
    Write-Log "FAIL  $Text"
}

# --- the Python runtime ---------------------------------------------------------------
#
# The tracker's analysis half is Python, and sending a player off to install it was the
# single largest thing standing between downloading this and having it work: the step
# most likely to be got wrong (see the App execution alias trap below) and the only one
# that involves a second website.
#
# So the installer fetches one if it has to - the official embeddable build from
# python.org, unpacked into ~/.ptcgl-tracker/python. That package is a plain zip holding
# a self-contained interpreter: no elevation, no PATH change, no registry entry, and
# nothing that can collide with a Python the user installs later.
#
# Three rules this follows:
#
#   * **Only the installer downloads.** Find-Python takes -Download and only install.ps1
#     passes it. The other scripts use a runtime that is already there and otherwise say
#     to run the installer; none should pull megabytes down on its own.
#   * **A Python already on the machine beats fetching one**, so nothing is downloaded
#     for a machine that does not need it.
#   * **But a runtime already fetched beats both**, so once a machine is bootstrapped it
#     keeps the same interpreter for good. A support question is answerable when everyone
#     who needed this is on one known build, and is not when the answer depends on what
#     else got installed in the meantime.

function Get-PtcglPythonRelease {
    <#
    The exact build the installer fetches. Version, URL and hash are pinned together and
    must be bumped together.

    The hash is what makes an automatic download safe: it is checked before anything is
    unpacked, so a truncated or substituted file is discarded rather than installed. Be
    straight about what it proves, though - it was taken from a fresh HTTPS download from
    www.python.org, so it guarantees every later install receives those same bytes. It is
    not an independent attestation by the Python project, which publishes no checksum of
    its own for this particular artifact.
    #>
    @{
        Version = "3.13.15"
        Url     = "https://www.python.org/ftp/python/3.13.15/python-3.13.15-embed-amd64.zip"
        Sha256  = "D1F04D990AEE1253D8569E8E5104E30FA9F5FA830899F14843448872D936A2CF"
        Bytes   = 11009825
    }
}

function Get-PtcglPythonRoot { Join-Path $env:USERPROFILE ".ptcgl-tracker\python" }
function Get-PtcglPythonExe  { Join-Path (Get-PtcglPythonRoot) "python.exe" }

function Test-PythonRuns {
    <#
    Does this path actually run a Python new enough to use?

    Existing on disk is not enough, for two separate reasons.

    Windows ships an "App execution alias" stub at
    %LOCALAPPDATA%\Microsoft\WindowsApps\python3.exe which is a real file on PATH even
    when Python is not installed - running it prints "Python was not found; run without
    arguments to install from the Microsoft Store" and exits non-zero. Get-Command finds
    that stub happily, so the only reliable test is to run it.

    And a Python that runs can still be too old: a 3.6 from years ago would fail later,
    somewhere much harder to connect back to the cause. 3.9 is the floor.
    #>
    param([string]$Path)
    try {
        $out = & $Path -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>&1
        if ($LASTEXITCODE -ne 0) { return $false }
        # The last non-blank line: a launcher may print something of its own first.
        $line = (("$out" -split "`r?`n") | Where-Object { $_.Trim() } | Select-Object -Last 1)
        $parts = "$line".Trim() -split '\.'
        if ($parts.Count -lt 2) { return $false }
        return ([int]$parts[0] -eq 3) -and ([int]$parts[1] -ge 9)
    } catch {
        return $false
    }
}

function Install-PtcglPython {
    <#
    Fetch the embeddable build into ~/.ptcgl-tracker/python and return the path to it.

    Nothing here needs elevation and nothing lands outside that one folder, so it is safe
    to re-run and safe to undo by deleting the folder.
    #>
    $rel = Get-PtcglPythonRelease
    $root = Get-PtcglPythonRoot
    $exe = Get-PtcglPythonExe

    if ((Test-Path $exe) -and (Test-PythonRuns $exe)) {
        Write-Detail "using the runtime already at $root"
        return $exe
    }

    Write-Step "Getting Python $($rel.Version) ($([math]::Round($rel.Bytes / 1MB)) MB, once)"
    Write-Detail "from $($rel.Url)"

    $zip = Join-Path ([IO.Path]::GetTempPath()) "ptcgl-python-$($rel.Version).zip"
    try {
        # PowerShell 5.1 still negotiates TLS 1.0 by default on some machines, which
        # python.org refuses outright - the download then fails with a bare "could not
        # create SSL/TLS secure channel" that says nothing about why.
        [Net.ServicePointManager]::SecurityProtocol =
            [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls11
    } catch { }

    # Invoke-WebRequest's progress bar makes a large download roughly ten times slower on
    # 5.1: it repaints the console on every chunk read.
    $prev = $ProgressPreference
    $ProgressPreference = "SilentlyContinue"
    try {
        Remove-Item $zip -Force -ErrorAction SilentlyContinue
        Invoke-WebRequest -Uri $rel.Url -OutFile $zip -UseBasicParsing -TimeoutSec 300
    } catch {
        throw ("Python could not be downloaded." +
               "`n  $($_.Exception.Message)" +
               "`n" +
               "`n  If this machine is offline or behind a filter, install Python yourself from" +
               "`n  https://www.python.org/downloads/ and run this again.")
    } finally {
        $ProgressPreference = $prev
    }

    # Verify before unpacking, never after: a file that is not what was asked for should
    # not be written into the tracker's own folder at all.
    $got = (Get-FileHash $zip -Algorithm SHA256).Hash
    if ($got -ne $rel.Sha256) {
        Remove-Item $zip -Force -ErrorAction SilentlyContinue
        throw ("The Python download did not match its checksum, so it was discarded." +
               "`n  expected $($rel.Sha256)" +
               "`n  got      $got" +
               "`n" +
               "`n  Try again. If it keeps happening, install Python yourself from" +
               "`n  https://www.python.org/downloads/")
    }
    Write-Detail "checksum OK"

    # Replace rather than merge, so a half-extracted attempt cannot leave a mixture of
    # two builds behind.
    Remove-Item $root -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    Expand-Archive -Path $zip -DestinationPath $root -Force
    Remove-Item $zip -Force -ErrorAction SilentlyContinue

    if (-not (Test-PythonRuns $exe)) {
        throw ("Python was downloaded but will not run from" +
               "`n    $root" +
               "`n  Install Python yourself from https://www.python.org/downloads/ and run this again.")
    }

    # A receipt, in the same shape and the same folder as install.json. Written with an
    # explicit BOM-free encoding, because Python's json.load rejects a BOM.
    try {
        $receipt = [ordered]@{
            version      = $rel.Version
            url          = $rel.Url
            sha256       = $rel.Sha256
            installed_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            platform     = "windows"
        } | ConvertTo-Json
        $utf8 = New-Object System.Text.UTF8Encoding $false
        [IO.File]::WriteAllText((Join-Path $root "python.json"), $receipt, $utf8)
    } catch { }

    Write-Detail "installed to $root"
    return $exe
}

function Find-Python {
    <#
    Returns a full path to a usable Python 3, or throws with something actionable.

    Order, and the reasoning for it, is in the note above Get-PtcglPythonRelease: a
    runtime this installer previously fetched, then the machine's own, then - only with
    -Download - a fresh fetch.

    Every candidate is executed before being accepted. Real installations are preferred
    over the WindowsApps alias, so a working Store Python is still used but never chosen
    ahead of a genuine one.
    #>
    param([switch]$Download)

    $mine = Get-PtcglPythonExe
    if ((Test-Path $mine) -and (Test-PythonRuns $mine)) { return $mine }

    $candidates = New-Object System.Collections.Generic.List[string]

    foreach ($name in "python3", "python", "py") {
        foreach ($cmd in (Get-Command $name -All -ErrorAction SilentlyContinue)) {
            if ($cmd.Source) { $candidates.Add($cmd.Source) }
        }
    }
    # Common install locations, in case Python exists but was never added to PATH -
    # the single most common way this fails for someone installing from python.org.
    foreach ($pattern in @(
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "$env:ProgramFiles\Python3*\python.exe",
        "${env:ProgramFiles(x86)}\Python3*\python.exe",
        "$env:LOCALAPPDATA\Microsoft\WindowsApps\python3.exe"
    )) {
        foreach ($hit in (Resolve-Path $pattern -ErrorAction SilentlyContinue)) {
            $candidates.Add($hit.Path)
        }
    }

    # Priority: a real python.exe, then the py launcher (which resolves to one), then the
    # WindowsApps alias last - it works only when a Store Python is actually installed.
    $ordered = $candidates | Select-Object -Unique | Sort-Object @{ Expression = {
        if ($_ -like "*\WindowsApps\*") { 2 }
        elseif ($_ -like "*\py.exe") { 1 }
        else { 0 }
    } }

    foreach ($c in $ordered) {
        if (Test-PythonRuns $c) { return $c }
    }

    if ($Download) { return Install-PtcglPython }

    # Only reachable from something that is not the installer, so the useful advice is to
    # run the installer - which fetches one - rather than to go and find Python by hand.
    throw ("No Python 3.9 or newer was found, and only the installer fetches one." +
           "`n  Run Install-Windows.cmd; it sets up Python for you if you have not got it.")
}

function Find-GameData {
    <#
    Locates the Unity "<Name>_Data" folder (the Windows equivalent of the macOS bundle's
    Contents/Resources/Data) given an optional hint: the game's .exe, its "*_Data"
    folder, or the folder it was installed into. Falls back to the known default
    install location. Returns $null if nothing was found.
    #>
    param([string]$Hint)

    $candidates = New-Object System.Collections.Generic.List[string]
    if ($Hint) { $candidates.Add($Hint) }

    # The real installer's default is a publisher folder under the user profile:
    #   %USERPROFILE%\The Pokemon Company International\Pokemon Trading Card Game Live
    # but with an accented "Pokemon" in both names. Matching the accent with a wildcard
    # keeps this file pure ASCII, so it cannot be broken by how PowerShell 5.1 decodes
    # an unmarked UTF-8 script.
    #
    # Resolve-Path before anything else: when the LAST segment of a path is a wildcard,
    # Get-ChildItem lists the entries *matching* it rather than the contents of the
    # directory it matched, which silently yields the wrong thing.
    foreach ($pattern in @(
        "$env:USERPROFILE\The Pok*mon Company International\Pok*mon Trading Card Game Live",
        "$env:USERPROFILE\*\Pok*mon Trading Card Game Live",
        "$env:LOCALAPPDATA\Programs\pokemon-trading-card-game-live",
        "$env:LOCALAPPDATA\Programs\Pokemon TCG Live",
        "$env:ProgramFiles\Pokemon TCG Live",
        "${env:ProgramFiles(x86)}\Pokemon TCG Live"
    )) {
        foreach ($hit in (Resolve-Path $pattern -ErrorAction SilentlyContinue)) {
            $candidates.Add($hit.Path)
        }
    }

    foreach ($c in $candidates) {
        if (-not $c -or -not (Test-Path -LiteralPath $c)) { continue }
        $item = Get-Item -LiteralPath $c

        if (-not $item.PSIsContainer -and $item.Extension -eq ".exe") {
            $data = Join-Path $item.Directory.FullName `
                ([IO.Path]::GetFileNameWithoutExtension($item.Name) + "_Data")
            if (Test-Path (Join-Path $data "Managed")) { return $data }
            continue
        }

        if ($item.PSIsContainer -and $item.Name -like "*_Data" `
                -and (Test-Path (Join-Path $item.FullName "Managed"))) {
            return $item.FullName
        }

        if ($item.PSIsContainer) {
            $dataDir = Get-ChildItem -LiteralPath $item.FullName -Directory -Filter "*_Data" `
                    -ErrorAction SilentlyContinue |
                Where-Object { Test-Path (Join-Path $_.FullName "Managed") } |
                Select-Object -First 1
            if ($dataDir) { return $dataDir.FullName }
        }
    }
    return $null
}
