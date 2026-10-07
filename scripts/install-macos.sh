#!/bin/bash
#
# Install the tracker into the game (macOS).
#
#     scripts/install-macos.sh [--game-path /Applications/...]
#
# The tracker loads as one of the game's own managed assemblies, registered in Unity's
# RuntimeInitializeOnLoads manifest. No BepInEx, no Doorstop: nothing here replaces or
# proxies a library, so there is no injector for macOS to object to. What there *is* on
# this platform, and is not on Windows, is a code signature over the whole bundle - see
# modinstall.reseal() for why that has to be put back and what happens if it is not.
#
# Idempotent - safe to re-run. After a game update the watcher normally puts the tracker
# back on its own; re-running this is the answer when it cannot.

set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
. "$HERE/macos-common.sh"

BUILD="$ROOT/mod/build"
BACKUP="$ROOT/backup/macos/manifests"
RECEIPT="$PTCGL_DATA/install.json"

GAME_HINT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --game-path) GAME_HINT="${2:-}"; shift 2 ;;
        *) shift ;;
    esac
done

PY=$(find_python) || exit 1

# --- find the game --------------------------------------------------------------------
if ! DATA=$(find_game_data "$GAME_HINT"); then
    fail "Could not find Pokemon TCG Live on this Mac.
If it is installed somewhere unusual, point at it:
  scripts/install-macos.sh --game-path '/path/to/Pokemon TCG Live.app'"
    exit 1
fi
step "Found the game"
detail "Game data: $DATA"

MANAGED="$DATA/Managed"
if [ ! -d "$MANAGED" ]; then
    fail "That does not look like the game folder - there is no Managed folder in $DATA."
    exit 1
fi
if [ ! -f "$BUILD/CbrewTracker.dll" ]; then
    fail "The tracker was not built. Run Install-macOS.command rather than this script directly."
    exit 1
fi

# --- the game must be closed ----------------------------------------------------------
#
# Windows gets this for free: the running .exe is locked by the OS, so a write simply
# fails. macOS locks nothing, so a copy into a live game silently succeeds and then the
# signature is re-computed underneath a process that is still mapping the old files. Ask
# first, rather than finding out.
if game_is_running "$PY" "$HERE" "$DATA"; then
    fail "Pokemon TCG Live is running. Quit the game and run this again.
Changing the game's files while it is open is how an install gets corrupted."
    exit 1
fi

# --- is another build of the tracker already in there? --------------------------------
#
# Two builds patch the same three methods and both append to the same events.jsonl, so
# together they record every match twice. Nothing about the game looks wrong when that
# happens, which is exactly why the installer has to be the one to catch it.
FOREIGN=$("$PY" "$HERE/modinstall.py" --foreign "$DATA")
if [ -n "$FOREIGN" ]; then
    step "Removing the older tracker already in the game"
    for name in $FOREIGN; do
        detail "removing $name"
        "$PY" "$HERE/modinstall.py" --remove "$DATA" --names "$name" 2>&1 | while IFS= read -r l; do detail "$l"; done
    done
fi

# --- back up the manifests (first run only) -------------------------------------------
detail "Backing up Unity manifests (first run only)"
mkdir -p "$BACKUP"
# The backup is only meaningful for the install it was taken from. Stamp it with that path
# and re-take when it does not match, or a backup captured against another copy of the
# game would one day be restored over a real one.
STAMP="$BACKUP/source.txt"
STALE=0
if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" != "$DATA" ]; then
    detail "existing backup was taken from a different install - re-taking"
    STALE=1
fi
for f in ScriptingAssemblies.json RuntimeInitializeOnLoads.json; do
    if [ "$STALE" = "1" ] || [ ! -f "$BACKUP/$f" ]; then
        cp "$DATA/$f" "$BACKUP/$f"
    fi
done
printf '%s' "$DATA" >"$STAMP"

# --- are the assemblies real? ---------------------------------------------------------
#
# Mono rejects metadata-only reference assemblies with "File does not contain a valid CIL
# image". NuGet hands out exactly such an assembly for Harmony (Lib.Harmony.Ref), so check
# here rather than discovering it from a game log later.
detail "Checking the assemblies are real CIL images"
if ! VERIFY=$("$PY" - "$BUILD" "$ROOT" <<'PYEOF' 2>&1
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(sys.argv[2]) / "ingest"))
from clrmeta import read_assembly, is_reference_assembly

build = pathlib.Path(sys.argv[1])
for name in ("CbrewTracker.dll", "0Harmony.dll"):
    path = build / name
    try:
        asm = read_assembly(str(path))
    except Exception as exc:
        sys.exit("    FAIL %s: not a readable .NET assembly (%s)" % (name, exc))
    if is_reference_assembly(str(path)):
        sys.exit("    FAIL %s: this is a reference assembly, not an implementation build. "
                 "Mono will reject it as 'not a valid CIL image'." % name)
    methods = sum(len(t.methods) for t in asm.values())
    print("    OK %s: %d types, %d methods" % (name, len(asm), methods))
PYEOF
); then
    fail "The tracker's files did not pass their own check, so nothing was copied into the game.
$VERIFY"
    exit 1
fi
detail "$VERIFY"

# --- keep a copy BEFORE touching the game ---------------------------------------------
#
# This is what makes an unattended repair possible at all. Without it the only copy of the
# tracker lives in the folder the user unzipped - which looks like leftovers and gets
# deleted - so a game update would leave nothing on the machine to restore from.
detail "Keeping a copy for future repairs"
if ! STASHED=$("$PY" "$HERE/modinstall.py" --stash "$BUILD" 2>&1); then
    fail "Could not keep a repair copy of the tracker's files.
$STASHED"
    exit 1
fi
detail "$STASHED"

# --- into the game --------------------------------------------------------------------
#
# Copying, registering and re-sealing all go through modinstall.py, which the watcher's
# repair path also calls. Two implementations of this would be two ways to produce a subtly
# different install, and the difference would show up only as hooks that quietly fail to
# attach - or, on this platform, as a game that stops launching.
detail "Copying assemblies into Managed/, registering with Unity, re-sealing the bundle"
if ! INSTALLED=$("$PY" "$HERE/modinstall.py" --apply "$DATA" --source "$BUILD" 2>&1); then
    fail "The tracker's files were copied but the game did not accept them.
$INSTALLED"
    exit 1
fi
detail "$INSTALLED"

# --- the receipt ----------------------------------------------------------------------
#
# Records where we installed, so the watcher's health check verifies this exact copy
# rather than re-guessing the location every time it runs.
mkdir -p "$PTCGL_DATA"
"$PY" - "$RECEIPT" "$DATA" <<'PYEOF'
import json, sys, time
json.dump({
    "game_data": sys.argv[2],
    "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "platform": "macos",
}, open(sys.argv[1], "w", encoding="utf-8"))
PYEOF

step "Installed the tracker"
detail "receipt: $RECEIPT"
detail "after the game runs, proof of a working install is a 'patched ...' line per hook in"
detail "$PTCGL_DATA/tracker.log"
