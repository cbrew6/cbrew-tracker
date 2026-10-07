#!/bin/bash
#
# One-step install (macOS).
#
#     scripts/install.sh [--game-path PATH] [--detail]
#
# Normally reached by double-clicking Install-macOS.command; run it directly only to pass
# --game-path. Finds the game, installs the tracker into it, and sets the watcher to run at
# login. After this, launch the game normally - nothing opens and nothing is displayed.
# Matches record and send in the background.
#
# A game update replaces the whole app, which removes the tracker and its Unity manifest
# entries. The watcher puts that back on its own - at the update itself, or within a couple
# of minutes of the game closing; re-running this is the answer when it cannot.
#
# --detail shows every step on the console. Without it you get four plain lines; the detail
# goes to ~/.ptcgl-tracker/install.log either way.

set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
cd "$ROOT"
. "$HERE/macos-common.sh"

GAME_PATH=""
DETAIL=""
while [ $# -gt 0 ]; do
    case "$1" in
        --detail) DETAIL=1; shift ;;
        --game-path) GAME_PATH="${2:-}"; shift 2 ;;
        *) shift ;;
    esac
done
if [ -n "$DETAIL" ]; then export PTCGL_INSTALL_DETAIL=1; fi
start_log

echo "cbrew Tracker - installing"
echo ""

# --- prerequisites ----------------------------------------------------------------------
if ! PY=$(find_python); then exit 1; fi
detail "Python: $PY ($("$PY" -V 2>&1))"

# --- the mod assemblies -------------------------------------------------------------------
# Prefer a local build if the toolchain and the game's own reference assemblies are both
# present, otherwise use the shipped copy - so installing needs no .NET SDK, which almost
# no Mac has.
BUILD="$ROOT/mod/build"
mkdir -p "$BUILD"

GAMELIBS="$ROOT/mod/libs/game"
if command -v dotnet >/dev/null 2>&1 && [ -d "$GAMELIBS" ] && [ -n "$(ls -A "$GAMELIBS" 2>/dev/null)" ]; then
    detail "Building from source"
    if ! OUT=$(cd "$ROOT/mod/CbrewTracker" && dotnet build -c Release -o ../build 2>&1); then
        detail "$OUT"
        fail "The tracker could not be built from source. See the log for what dotnet said."
        exit 1
    fi
    detail "built"
elif [ -f "$ROOT/mod/dist/CbrewTracker.dll" ] && [ -f "$ROOT/mod/dist/0Harmony.dll" ]; then
    detail "Using the shipped build (no .NET toolchain needed)"
    cp "$ROOT/mod/dist/CbrewTracker.dll" "$ROOT/mod/dist/0Harmony.dll" "$BUILD/"
else
    fail "This copy of the tracker is incomplete - mod/dist/ has no assemblies in it.
Download it again from the releases page."
    exit 1
fi

# --- into the game ------------------------------------------------------------------------
# Any running watcher is stopped first, this build's or another's. One left running while
# the game's files change could put back exactly what this install replaces; the watcher
# step below starts a fresh one.
"$HERE/install-watcher-macos.sh" --stop >/dev/null 2>&1 || true
if [ -n "$GAME_PATH" ]; then
    "$HERE/install-macos.sh" --game-path "$GAME_PATH"
else
    "$HERE/install-macos.sh"
fi

# --- the watcher --------------------------------------------------------------------------
"$HERE/install-watcher-macos.sh"

# --- send anything already recorded --------------------------------------------------------
#
# A first submission proves the whole chain works before the player ever launches the game,
# and on a machine that already has history it backfills the lot.
#
# A failure here is cosmetic and must never fail the install. The tracker is in place by
# this point and the watcher retries on its own - failing to send is never failing to
# record.
if OUT=$("$PY" "$ROOT/ingest/ingest.py" 2>&1) && detail "$OUT" \
   && OUT=$("$PY" "$ROOT/analysis/submit.py" 2>&1) && detail "$OUT"; then
    step "Sent what you had recorded"
else
    detail "nothing sent now: $OUT"
    step "Your matches will send after the next game"
fi

echo ""
echo "Done. Launch Pokemon TCG Live and play."
echo "Nothing to open - this client records and sends in the background."
log_line "OK    install finished"
