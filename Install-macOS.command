#!/bin/bash
# Double-click this to install (or repair) the tracker. Nothing to type.
#
# A Pokemon TCG Live update removes the tracker, and the tracker puts itself back. Run this
# again only if it ever tells you it could not.
#
# If macOS refuses to open this file because it "is from an unidentified developer",
# right-click it and choose Open instead of double-clicking. That is a one-off; macOS
# remembers the answer.

cd "$(dirname "$0")" || exit 1

# install.sh prints the whole story, success or failure, in plain words. Nothing is added
# on top of it here - a second banner is how a run ends with two different "Done" messages.
./scripts/install.sh "$@"
RC=$?

echo ""
# Only when a person is watching. Terminal can be set to close the window the moment the
# script exits, which would take the result with it - but a prompt in a scripted run would
# hang forever instead.
if [ -t 0 ]; then
    printf 'Press return to close this window. '
    read -r _
fi
exit $RC
