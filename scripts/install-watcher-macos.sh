#!/bin/bash
#
# Run the match watcher automatically, so launching the game is all you have to do (macOS).
#
#     scripts/install-watcher-macos.sh          install and start it
#     scripts/install-watcher-macos.sh --stop   stop and remove it
#
# Installs a per-user LaunchAgent that starts scripts/watch.py at login and restarts it if
# it dies. The watcher reads the tracker's event log and sends matches on; it does not
# touch the game, so leaving it running costs nothing.
#
# Unlike the Windows watcher, this does NOT point at the repo checkout, and that is not a
# preference. A LaunchAgent is a background process with no interface, and macOS will not
# let one read ~/Documents, ~/Desktop or ~/Downloads without a consent dialog it has no way
# to raise. A watcher pointed at a checkout in any of those three folders is denied its own
# source file and dies at startup, forever, silently. So the handful of Python files it
# needs are copied into ~/.ptcgl-tracker/bin, which is not a protected location.
#
# The cost of that is a deployed copy which can go stale: editing the checkout changes
# nothing until this script is run again.

set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
. "$HERE/macos-common.sh"

LABEL="com.cbrew.tracker.watcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
BIN="$PTCGL_DATA/bin"
LOGFILE="$PTCGL_DATA/watcher.log"
CRASHLOG="$PTCGL_DATA/watcher-startup.log"

unload_agent() {
    # bootout first, then the old verb. `launchctl unload` still works on current macOS
    # but prints a deprecation notice; `bootout` is the documented replacement and fails
    # harmlessly when the job is not loaded.
    launchctl bootout "gui/$(id -u)/$1" >/dev/null 2>&1 || true
    launchctl unload -w "$2" >/dev/null 2>&1 || true
}

stop_everything() {
    unload_agent "$LABEL" "$PLIST"
    # Agents another build of this tracker registered under its own label. Both would
    # watch the same events.jsonl, and the other would keep putting its own build back
    # into the game, so only one may ever be registered.
    for other in "$HOME"/Library/LaunchAgents/com.cbrew.ptcgl-tracker*.plist; do
        [ -f "$other" ] || continue
        other_label=$(basename "$other" .plist)
        detail "removing another build's watcher ($other_label)"
        unload_agent "$other_label" "$other"
        rm -f "$other"
    done
    # Anything still polling, however it was started. Without this a re-run leaves the old
    # process alive beside the new one, and two watchers submit the same match twice.
    pkill -f "$PTCGL_DATA/bin/watch.py" >/dev/null 2>&1 || true
    pkill -f "$ROOT/scripts/watch.py" >/dev/null 2>&1 || true
}

if [ "${1:-}" = "--stop" ]; then
    stop_everything
    rm -f "$PLIST"
    echo "Watcher stopped and removed."
    exit 0
fi

PY=$(find_python) || exit 1

stop_everything

# --- deploy the files the watcher needs ------------------------------------------------
#
# watch.py resolves both layouts on its own: in the checkout it looks one level up for
# ingest/ and analysis/, and here it finds them beside itself.
detail "Deploying the watcher to $BIN"
rm -rf "$BIN"
mkdir -p "$BIN/ingest" "$BIN/analysis"
cp "$ROOT/scripts/watch.py" "$ROOT/scripts/healthcheck.py" "$ROOT/scripts/modinstall.py" "$BIN/"
cp "$ROOT/ingest/ingest.py" "$ROOT/ingest/clrmeta.py" "$BIN/ingest/"
cp "$ROOT/analysis/submit.py" "$ROOT/analysis/seasons.py" "$BIN/analysis/"
# VERSION too: submit.py reads it from one level above analysis/, and without it every match
# the watcher sends would report the client as "unknown".
cp "$ROOT/VERSION" "$BIN/"
detail "deployed $(find "$BIN" -name '*.py' | wc -l | tr -d ' ') files"

# --- the relauncher -------------------------------------------------------------------------
#
# What Pokemon's updater opens at the end of a game update, instead of the game. The mod's
# UpdaterHandoffHook points the updater's --launchAppAt here when the game starts its
# updater; once the new version is copied in, this puts the tracker back and then opens the
# game. The updated game therefore starts with the tracker already in it - no restart, and
# nothing on screen that was not there before.
#
# A bundle, because the updater hands over with `open "<path>"`, and `open` launches apps.
# LSBackgroundOnly keeps it out of the Dock and the app switcher: there is nothing in it to
# look at. The executable is a shell script, which LaunchServices runs like any other; being
# made here rather than downloaded, it carries no quarantine and Gatekeeper has nothing to
# assess. The mod checks this executable exists before handing over, so a missing or half-
# written relauncher costs the seamless re-attach and never the game opening.
RELAUNCHER="$PTCGL_DATA/cbrew Tracker.app"
detail "Building the relauncher at $RELAUNCHER"
rm -rf "$RELAUNCHER"
mkdir -p "$RELAUNCHER/Contents/MacOS"
cat >"$RELAUNCHER/Contents/Info.plist" <<INFO_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>      <string>com.cbrew.tracker.relauncher</string>
    <key>CFBundleName</key>            <string>cbrew Tracker</string>
    <key>CFBundleDisplayName</key>     <string>cbrew Tracker</string>
    <key>CFBundleExecutable</key>      <string>relaunch</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>CFBundleVersion</key>         <string>1</string>
    <key>CFBundleShortVersionString</key><string>1</string>
    <key>LSBackgroundOnly</key>        <true/>
    <key>LSMinimumSystemVersion</key>  <string>11.0</string>
</dict>
</plist>
INFO_EOF
# Unquoted heredoc only for the one value baked in at install time - the interpreter this
# installer already proved works. Everything else in the script is escaped so it runs later,
# at hand-off time, not now.
cat >"$RELAUNCHER/Contents/MacOS/relaunch" <<RELAUNCH_EOF
#!/bin/bash
# cbrew Tracker relauncher (macOS). Generated by scripts/install-watcher-macos.sh.
#
# Pokemon's updater opens this instead of the game when an update has been copied in (see
# mod/CbrewTracker/Hooks/UpdaterHandoffHook.cs). It puts the tracker back into the
# updated game, then opens the game.
#
# **It opens the game whatever else happens.** That is the trap on EXIT: a repair that
# fails, a Python that will not start, a script error - every path ends with the game
# opening. An update that finishes and leaves the game closed would be a far worse outcome
# than one that opens the game without the tracker, which the watcher then fixes.

DATA="\$HOME/.ptcgl-tracker"
LOG="\$DATA/relauncher.log"
PY="$PY"
BIN="\$DATA/bin"
GAME_ID="com.pokemon.pokemontcgl"

log() { printf '[%s] %s\n' "\$(date '+%Y-%m-%d %H:%M:%S')" "\$1" >>"\$LOG" 2>/dev/null; }

APP=""
OPENED=""
open_game() {
    [ -n "\$OPENED" ] && return
    OPENED=1
    if [ -n "\$APP" ] && [ -d "\$APP" ]; then
        log "opening \$APP"
        open "\$APP" && return
        log "could not open \$APP - trying the game's bundle id instead"
    fi
    log "opening the game by its bundle id"
    open -b "\$GAME_ID" || log "could not open the game at all - open it yourself"
}
trap open_game EXIT

mkdir -p "\$DATA" 2>/dev/null
# Keep the log readable: one long-lived file, trimmed rather than left to grow.
if [ -f "\$LOG" ] && [ "\$(wc -c <"\$LOG" | tr -d ' ')" -gt 262144 ]; then : >"\$LOG"; fi
log "======================================================"
log "Pokemon's updater handed over - putting the tracker back before the game opens"

# Which game to open, most specific first: what the updater itself was about to open (the
# mod writes it down at hand-off), then the install receipt, then - in open_game - the
# game's bundle id, which needs no path at all.
APP=\$("\$PY" -I -c '
import json, os, sys
d = os.path.expanduser("~/.ptcgl-tracker")
try:
    a = json.load(open(os.path.join(d, "handoff.json"), encoding="utf-8-sig")).get("launch_app_at")
    if a and os.path.isdir(a):
        print(a); sys.exit()
except Exception:
    pass
try:
    g = json.load(open(os.path.join(d, "install.json"), encoding="utf-8-sig")).get("game_data") or ""
    app = os.path.dirname(os.path.dirname(os.path.dirname(g)))
    if app.endswith(".app") and os.path.isdir(app):
        print(app)
except Exception:
    pass
' 2>>"\$LOG")
log "game: \${APP:-(not found - will open by bundle id)}"

# The repair itself is modinstall.py, the same code the installer and the watcher use, so a
# repair after an update cannot drift from a fresh install. --wait covers the updater still
# shutting down: it opens this and only then quits.
#
# Never killed. An interrupted re-sign could leave the game's executable half-rewritten,
# which on Apple Silicon is a game that will not start. A repair that somehow hangs gets
# three minutes, after which the game is opened anyway and the repair left to finish.
"\$PY" -I "\$BIN/modinstall.py" --repair --wait 60 >>"\$LOG" 2>&1 &
REPAIR=\$!
for _ in \$(seq 1 180); do
    kill -0 "\$REPAIR" 2>/dev/null || break
    sleep 1
done
if kill -0 "\$REPAIR" 2>/dev/null; then
    log "repair still running after three minutes - opening the game anyway"
else
    wait "\$REPAIR"
    RC=\$?
    case "\$RC" in
        0) log "tracker is back in the game" ;;
        2) log "repair skipped (reason above) - the watcher will retry once the game is closed" ;;
        *) log "repair failed (exit \$RC) - the watcher will retry once the game is closed" ;;
    esac
fi
rm -f "\$DATA/handoff.json"
# open_game runs from the EXIT trap.
RELAUNCH_EOF
chmod +x "$RELAUNCHER/Contents/MacOS/relaunch"
if ! plutil -lint "$RELAUNCHER/Contents/Info.plist" >/dev/null 2>&1 || ! bash -n "$RELAUNCHER/Contents/MacOS/relaunch"; then
    # The mod only hands over to a relauncher whose executable exists, so removing a bad one
    # is enough to make updates fall back to the watcher rather than to a broken hand-off.
    rm -rf "$RELAUNCHER"
    detail "the relauncher came out malformed and was removed - updates will re-attach on the next game close instead"
else
    detail "relauncher ready"
fi

# --- the LaunchAgent --------------------------------------------------------------------
#
# --log rather than StandardOutPath: watch.py opens the file itself, in UTF-8 and line
# buffered, which is what lets the log be read while the watcher is running. StandardError
# still goes somewhere, though, and to a *different* file - it catches the one class of
# failure the watcher cannot log for itself, which is dying before it gets as far as
# opening its log. An import error with nowhere to land is how a watcher disappears
# without explanation.
mkdir -p "$HOME/Library/LaunchAgents" "$PTCGL_DATA"
cat >"$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$BIN/watch.py</string>
        <string>--log</string>
        <string>$LOGFILE</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$BIN</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardErrorPath</key>
    <string>$CRASHLOG</string>
</dict>
</plist>
PLIST_EOF

# plutil before launchctl: a malformed plist is rejected with a message about the file
# rather than about what is wrong with it, and this is a generated file so a mistake here
# would hit every machine at once.
if ! plutil -lint "$PLIST" >/dev/null 2>&1; then
    fail "The watcher's launch settings came out malformed and were not installed:
  $PLIST"
    exit 1
fi

if ! launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
    # Older verb, for a macOS where bootstrap is unavailable or the job is somehow still
    # registered. One of the two works everywhere this runs.
    launchctl load -w "$PLIST" >/dev/null 2>&1 || true
fi
launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true

sleep 2
if pgrep -f "$BIN/watch.py" >/dev/null 2>&1; then
    RUNNING="yes, pid $(pgrep -f "$BIN/watch.py" | tr '\n' ' ')"
else
    RUNNING="not detected - check $CRASHLOG"
fi

step "Set it to start when you log in"
detail "LaunchAgent: $PLIST"
detail "running now: $RUNNING"
detail "log: $LOGFILE"
detail "stop with: scripts/install-watcher-macos.sh --stop"
