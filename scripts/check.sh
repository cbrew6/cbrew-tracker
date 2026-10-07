#!/bin/bash
#
# Is the tracker still installed and recording? (macOS)
#
#     scripts/check.sh
#
# For troubleshooting, run from a terminal. Reads only - it changes nothing, and is safe to
# run while the game is open.
#
# It goes through find_python rather than a bare `python3` for the reason spelled out in
# macos-common.sh: on a Mac without the Command Line Tools, /usr/bin/python3 is a stub that
# opens a dialog instead of running. A health check that reports a failure - or worse, hangs
# on a dialog - because it could not find its own interpreter is worse than no health check.

set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
cd "$ROOT"

# Only for find_python. Sourcing defines functions and runs nothing, and start_log is
# deliberately not called: the install log is a record of installs, and a health check in it
# would only make a support log harder to read.
. "$HERE/macos-common.sh"

if ! PY=$(find_python); then
    echo ""
    echo "The health check could not start."
    echo ""
    exit 1
fi

"$PY" "$HERE/healthcheck.py" "$@"
