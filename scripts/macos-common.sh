# Shared helpers for the macOS install/uninstall/check scripts. Source this file; it
# defines functions only and does nothing on its own.
#
# Written for bash 3.2, which is the bash macOS still ships in /bin. No associative
# arrays, no ${var,,}, no mapfile - a script that needs bash 4 works on the developer's
# Homebrew machine and fails on everybody else's.

# --- what the person installing actually sees -----------------------------------------
#
# Same bargain as the Windows installer: about four lines a player understands on the
# console, and everything else to ~/.ptcgl-tracker/install.log. The detail is not deleted,
# it is demoted - it is what makes a failure diagnosable - so when someone reports a
# problem, ask for that file rather than a screenshot.

PTCGL_DATA="$HOME/.ptcgl-tracker"
PTCGL_LOG="$PTCGL_DATA/install.log"

log_line() {
    # Never fails an install because the log could not be written.
    mkdir -p "$PTCGL_DATA" 2>/dev/null || return 0
    printf '%s\n' "$1" >>"$PTCGL_LOG" 2>/dev/null || true
}

start_log() {
    # One run header per process, not per script: the child scripts source this file too.
    if [ -n "${PTCGL_INSTALL_LOG_OPEN:-}" ]; then return 0; fi
    export PTCGL_INSTALL_LOG_OPEN=1
    mkdir -p "$PTCGL_DATA" 2>/dev/null || true
    # A support log nobody prunes still should not grow without limit.
    if [ -f "$PTCGL_LOG" ]; then
        size=$(wc -c <"$PTCGL_LOG" 2>/dev/null | tr -d ' ')
        if [ -n "$size" ] && [ "$size" -gt 262144 ]; then rm -f "$PTCGL_LOG"; fi
    fi
    log_line ""
    log_line "=== $(date '+%Y-%m-%d %H:%M:%S') install run"
}

step() {
    # One line a player understands. There should be about four of these in a run.
    printf '  %s\n' "$1"
    log_line "STEP  $1"
}

detail() {
    # Everything else: always logged, shown only with --detail.
    printf '%s\n' "$1" | while IFS= read -r line; do
        [ -z "$(printf '%s' "$line" | tr -d '[:space:]')" ] && continue
        if [ -n "${PTCGL_INSTALL_DETAIL:-}" ]; then printf '      %s\n' "$line"; fi
        log_line "      $line"
    done
    return 0
}

fail() {
    # The only failure message. Plain words, then where to look.
    printf '\n'
    printf 'Install did not finish.\n'
    printf '\n'
    printf '%s\n' "$1" | while IFS= read -r line; do printf '  %s\n' "$line"; done
    printf '\n'
    printf '  Your game was not changed.\n'
    printf '  Details: %s\n' "$PTCGL_LOG"
    log_line "FAIL  $1"
}

# --- the Python runtime ---------------------------------------------------------------
#
# Unlike the Windows installer, this one never downloads a runtime. It does not have to:
# every Mac ships python3 at /usr/bin/python3, and what python.org offers instead is a
# .pkg that wants an administrator password - a far worse thing to spring on someone than
# the Windows embeddable zip, which unpacks into the tracker's own folder and touches
# nothing else.
#
# /usr/bin/python3 has a trap of its own, the same shape as the Windows App execution
# alias. It is a *stub*: on a Mac without the
# Command Line Tools it is a real file on PATH that, when run, pops a GUI dialog offering
# to install them and blocks until somebody clicks. An installer that hangs on a dialog
# nobody was expecting is worse than one that says what is missing, so the stub is
# detected up front with xcode-select rather than by running it and hoping.

python_runs() {
    # Does this path run a Python new enough to use? 3.9 is the floor, matching Windows.
    [ -x "$1" ] || return 1
    out=$("$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || return 1
    major=$(printf '%s' "$out" | cut -d. -f1)
    minor=$(printf '%s' "$out" | cut -d. -f2)
    [ "$major" = "3" ] || return 1
    [ -n "$minor" ] || return 1
    [ "$minor" -ge 9 ] || return 1
    return 0
}

clt_installed() {
    # The Command Line Tools (or a full Xcode) are what make /usr/bin/python3 real rather
    # than a stub that opens a dialog.
    xcode-select -p >/dev/null 2>&1
}

find_python() {
    # Prints a full path to a usable Python 3, or returns 1 having said what is missing.
    #
    # Homebrew and python.org first, /usr/bin last. Not a judgement about which is better:
    # /usr/bin/python3 is the one that can be a stub, so it is the one to reach for only
    # after the candidates that cannot be.
    for candidate in \
        /opt/homebrew/bin/python3 \
        /usr/local/bin/python3 \
        "$(command -v python3 2>/dev/null)" \
        /Library/Frameworks/Python.framework/Versions/Current/bin/python3
    do
        [ -n "$candidate" ] || continue
        case "$candidate" in /usr/bin/python3) continue ;; esac
        if python_runs "$candidate"; then printf '%s\n' "$candidate"; return 0; fi
    done

    if clt_installed && python_runs /usr/bin/python3; then
        printf '%s\n' /usr/bin/python3
        return 0
    fi

    printf '%s\n' \
        "No Python 3.9 or newer was found on this Mac." \
        "" \
        "  The quickest fix is Apple's own, which needs no download page:" \
        "    xcode-select --install" \
        "" \
        "  That installs the Command Line Tools, which include Python 3." \
        "  Then run this installer again." >&2
    return 1
}

# --- the game --------------------------------------------------------------------------

find_game_data() {
    # Prints the Unity data folder inside the game bundle, or returns 1.
    #
    # A macOS Unity player keeps it at <App>.app/Contents/Resources/Data - the equivalent
    # of <Name>_Data\ on Windows. Accepts a hint that is the .app itself, the Data folder,
    # or a folder the app sits in.
    #
    # Every pattern spells the name "Pok*mon" on purpose. The real bundle has an accented
    # e, this file stays ASCII so nothing can mangle it in transit, and the wildcard
    # matches the app whichever way the name is written.
    hint="$1"

    for candidate in \
        "$hint" \
        /Applications/Pok*mon\ TCG\ Live.app \
        "$HOME"/Applications/Pok*mon\ TCG\ Live.app \
        /Applications/Pok*mon\ Trading\ Card\ Game\ Live.app \
        "$HOME"/Applications/Pok*mon\ Trading\ Card\ Game\ Live.app
    do
        [ -n "$candidate" ] || continue
        [ -e "$candidate" ] || continue

        # Already the data folder.
        if [ -d "$candidate/Managed" ]; then
            printf '%s\n' "$candidate"; return 0
        fi
        # An .app bundle.
        if [ -d "$candidate/Contents/Resources/Data/Managed" ]; then
            printf '%s\n' "$candidate/Contents/Resources/Data"; return 0
        fi
        # A folder holding one.
        for inner in "$candidate"/*.app; do
            if [ -d "$inner/Contents/Resources/Data/Managed" ]; then
                printf '%s\n' "$inner/Contents/Resources/Data"; return 0
            fi
        done
    done
    return 1
}

game_is_running() {
    # game_is_running <python> <scripts_dir> <game_data>
    #
    # Delegates to modinstall.py so the installer and the watcher's unattended repair
    # agree on what "running" means. Two answers to that question would mean the installer
    # writing into a live game, or the repair refusing forever.
    "$1" -c 'import sys; sys.path.insert(0, sys.argv[1]); import modinstall; sys.exit(0 if modinstall.game_running(sys.argv[2]) else 1)' "$2" "$3"
}
