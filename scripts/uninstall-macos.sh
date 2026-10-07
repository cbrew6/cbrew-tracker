#!/bin/bash
#
# Remove the tracker from the game and stop the watcher (macOS).
#
#     scripts/uninstall-macos.sh
#
# Your match data in ~/.ptcgl-tracker is untouched. Only the tracker itself is removed.

set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
. "$HERE/macos-common.sh"

PY=$(find_python) || exit 1

echo "==> Stopping the watcher"
"$HERE/install-watcher-macos.sh" --stop
rm -rf "$PTCGL_DATA/bin"
echo "    removed the deployed copy"

echo "==> Removing the tracker from the game"
DATA=""
if [ -f "$PTCGL_DATA/install.json" ]; then
    DATA=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8-sig")).get("game_data") or "")' "$PTCGL_DATA/install.json" 2>/dev/null || true)
fi
if [ -z "$DATA" ] || [ ! -d "$DATA" ]; then
    DATA=$(find_game_data "" || true)
fi

if [ -z "$DATA" ] || [ ! -d "$DATA" ]; then
    echo "    the game was not found - nothing to remove from it"
elif game_is_running "$PY" "$HERE" "$DATA"; then
    echo "    Pokemon TCG Live is running. Quit the game and run this again."
    exit 1
else
    # Surgical removal, then the seal put back over what is left. Removing the files and
    # leaving the signature describing a bundle that no longer exists would be its own
    # problem - and a worse one, because it stops the game launching rather than stopping
    # it recording.
    "$PY" "$HERE/modinstall.py" --remove "$DATA" | sed 's/^/    /'
fi

# Take away what a repair would need, so nothing can put the tracker back. The watcher is
# stopped above, but the relauncher repairs too. Removing the receipt makes
# healthcheck.check() report "no install receipt", which repairable() deliberately refuses
# to act on; the stash goes too, so nothing is left to reinstall from.
echo "==> Removing the install receipt and repair copy"
# The relauncher goes with them. Without the mod nothing hands an update over to it, but a
# stray app in the data folder that tries to re-install the tracker has no business staying.
for leftover in "$PTCGL_DATA/install.json" "$PTCGL_DATA/mod" "$PTCGL_DATA/tracker-alert.html" \
                "$PTCGL_DATA/cbrew Tracker.app" "$PTCGL_DATA/handoff.json"; do
    if [ -e "$leftover" ]; then
        rm -rf "$leftover"
        echo "    removed $(basename "$leftover")"
    fi
done

echo ""
echo "Done. Your match data in $PTCGL_DATA is untouched."
echo "Deleting that folder removes everything the tracker ever kept on this Mac."
