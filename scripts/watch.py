#!/usr/bin/env python3
"""Watch for matches, ingest them, and send them to the leaderboard.

The whole background runtime. Nothing renders - the loop that matters is:

    events.jsonl grows  ->  ingest.py  ->  SQLite  ->  submit.py  ->  the leaderboard

**A match is sent twice: once when it starts and again when it ends.** The first carries
everything known before a card is played - both players, both ratings, your own deck - and
the second adds the result and the shape of the game. Same match_id, so the server upserts
one row; resubmission is free and this is the same path.

Three properties, each load-bearing:

  * **The log is opened by this process, in UTF-8.** The watcher runs under `pythonw.exe`,
    which allocates no console - the only way to have genuinely no window rather than a
    hidden or minimised one. Under pythonw `sys.stdout` is None, so open_log() must run
    before anything prints.
  * **The install is health-checked every two minutes, and only the transition is
    announced.** A game update silently removes the assemblies; the game then plays
    perfectly normally and records nothing, and a client with no interface is exactly
    where that would go unnoticed longest.
  * **Failing to send is never failing to record.** Submission runs in its own process and
    every path returns rather than raises. The event log on disk is the product.
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import webbrowser

# This file runs from two places, and has to find its own siblings in both.
#
#   the checkout      <repo>/scripts/watch.py     with ingest/ and analysis/ one level up
#   the deployed copy ~/.ptcgl-tracker/bin/watch.py   with ingest/ and analysis/ beside it
#
# The deployed copy exists for macOS. A LaunchAgent is an ordinary background process with
# no interface, and macOS will not let one read ~/Documents, ~/Desktop or ~/Downloads
# without consent it has no way to ask for - so a watcher pointed at a checkout in any of
# those folders is denied its own source file and never starts. Copying what it needs into
# ~/.ptcgl-tracker, which is not a protected location, sidesteps TCC entirely. Windows has
# no equivalent restriction and points straight at the checkout.
_here = os.path.dirname(os.path.abspath(__file__))
if os.path.isdir(os.path.join(_here, "ingest")):
    ROOT = _here                                  # deployed: everything is beside us
    _modules = _here
else:
    ROOT = os.path.dirname(_here)                 # the checkout
    _modules = os.path.join(ROOT, "scripts")
sys.path.insert(0, _modules)

import healthcheck                                              # noqa: E402
import modinstall                                               # noqa: E402

DATA = os.path.expanduser("~/.ptcgl-tracker")
EVENTS = os.path.join(DATA, "events.jsonl")

# How many times to try repairing before giving up and showing the alert instead. A repair
# that fails twice is failing for a reason retrying will not change - usually a game folder
# this process cannot write to - and grinding at it every two minutes helps nobody.
MAX_REPAIR_ATTEMPTS = 2

# How often to re-check that the tracker is still installed in the game.
HEALTH_EVERY = 120.0

# Written by the mod (UpdaterHandoffHook) when the game hands a game update to Pokemon's
# updater, and removed by the relauncher once it has put the tracker back.
HANDOFF = os.path.join(DATA, "handoff.json")

# How long a recorded hand-off excuses a broken-looking install. The updater waits for its
# Play button, so this has to cover a person taking their time (eight minutes, the first
# time it ran for real) - and be short enough that an update abandoned half-way, the updater
# closed without Play, is still announced the same day.
HANDOFF_GRACE = 30 * 60.0


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run(script, *args):
    """Run one of our own tools, returning (ok, combined output)."""
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, script), *args],
        capture_output=True, text=True,
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def open_log(path):
    """Send stdout and stderr to `path`, and keep going if that is impossible.

    Line buffered because the log is read while the watcher is running, and a failure to
    open it must not stop the watcher: recording matches matters more than logging that it
    did.
    """
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        handle = open(path, "a", encoding="utf-8", buffering=1, errors="replace")
    except OSError:
        return
    sys.stdout = handle
    sys.stderr = handle


def new_events(path, offset):
    """Read events appended since `offset`. Returns (events, new_offset)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset

    if size < offset:          # log was replaced or trimmed; start over
        offset = 0
    if size == offset:
        return [], offset

    events = []
    with open(path, "r", encoding="utf-8") as fh:
        fh.seek(offset)
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass           # a half-written line; the next poll picks it up
        offset = fh.tell()
    return events, offset


def health_watch(quiet):
    """Poll the install, repair it when it can be repaired, and shout about what is left.

    Only the transition is announced: a broken install stays broken until it is fixed, and
    reopening the alert every two minutes would be its own problem.

    **This is the one thing this client puts on screen**, and it carries no match data - it
    says the tracker has stopped recording. Hiding it would be the wrong reading of "no
    interface": what is withheld is match data, not the news that the software has stopped
    working.

    A repair pass runs in front of it. A game update removes the tracker every single time,
    and "run the installer again" assumes the person still has the installer and knows what
    it was for. Almost every breakage is that same one, and the machine already holds
    everything needed to undo it: the game's location in the receipt, and the assemblies in
    the stash.

    **What the alert says has to match what the repair is doing.** A repair waiting on the
    game to close still needs announcing - nothing is being recorded while it waits - but
    announcing it with "go and run the installer" would tell somebody to do by hand the
    thing that is about to happen on its own. So a repair says whether it is merely deferred
    (`RepairDeferred`), which is a page saying "close the game and wait", against a page
    saying "this one needs you". The page is also retracted when the install comes back: it
    is a file, so the tab left open on it is what somebody returns to and acts on, long
    after it has stopped being true.
    """
    was_seen = False        # has a page been shown for the state we are currently in?
    alert_mode = None       # what that page says, so it can be rewritten when that changes
    failed_repairs = 0
    repaired_itself = False
    said_deferred = False   # a long play session is 150 passes; say it once, not 150 times
    awaiting_proof = False  # a repair has run, and no game has started to prove it yet
    while True:
        try:
            status = healthcheck.check()
        except Exception as exc:                     # never take the watcher down
            log(f"health check failed to run: {exc}")
            time.sleep(HEALTH_EVERY)
            continue

        # Repair before shouting, and only announce what the repair could not fix. Fixing
        # it quietly and then opening an alarming page about it anyway would be worse than
        # the bug.
        deferred = False
        if (not status["ok"]
                and failed_repairs < MAX_REPAIR_ATTEMPTS
                and healthcheck.repairable(status)):
            try:
                changed = modinstall.repair(status["game_data"], log=log)
                if changed:
                    log("repaired the install - start the game and this log will say "
                        "whether it held")
                    repaired_itself = True
                    awaiting_proof = True
                failed_repairs = 0
                said_deferred = False
                status = healthcheck.check()          # report on what is true NOW
            except modinstall.RepairDeferred as why:
                # Not a failure, and not something to hand back to the user either. The
                # commonest case by far is "the game is running", and the pass two minutes
                # from now will find it closed.
                deferred = True
                if not said_deferred:
                    log(f"health: not repairing yet - {why}")
                    said_deferred = True
            except modinstall.RepairSkipped as why:
                # Skipped for a reason that will still be true in two minutes - no stashed
                # copy, most likely. Nothing changes until a person does something.
                log(f"health: cannot repair from here - {why}")
            except Exception as exc:
                # A real failure - most likely the game folder needs elevation this process
                # does not have. Try a couple of times in case it was transient, then stop
                # and let the alert do its job rather than grinding every two minutes.
                failed_repairs += 1
                log(f"repair attempt {failed_repairs} failed: {exc}")
                if failed_repairs >= MAX_REPAIR_ATTEMPTS:
                    log("cannot repair automatically - falling back to the alert")

        # **Putting the files back is not proof that the tracker works.** That is only
        # known once the game next starts and the mod says what it managed to patch, and
        # until then the newest log is from before the update that broke everything. This
        # is the line somebody should be able to find before deciding whether to go and
        # reinstall by hand - and, if the hooks did not hold, the check has by now added a
        # problem no repair can fix, which takes the alert to "this one needs you".
        if awaiting_proof and status["hooks_confirmed"] is not None:
            if status["hooks_confirmed"]:
                log(f"repair confirmed by a game launch: {healthcheck.describe(status)}")
            else:
                log(f"the repair did not hold: {healthcheck.describe(status)}")
            awaiting_proof = False

        # **A game update being handed over is not news.** Mid-copy, the manifests Pokemon's
        # updater has already overwritten make the install look broken, and the repair is
        # deferred because the updater is running - but the mod has pointed the updater's
        # hand-off at the relauncher, which puts the tracker back before the game opens.
        # Announcing it would be an alarm for a fire already being put out: a "not
        # recording" page opening mid-copy, minutes before the relauncher fixes everything in
        # a second. Nothing can be lost meanwhile either - the game is closed while its
        # updater runs.
        if deferred and handoff_in_progress():
            time.sleep(HEALTH_EVERY)
            continue

        if status["ok"]:
            mode = None
        elif deferred and failed_repairs < MAX_REPAIR_ATTEMPTS:
            mode = healthcheck.PENDING
        else:
            mode = healthcheck.MANUAL

        if not was_seen or mode != alert_mode:
            if mode is None:
                log(f"health: {healthcheck.describe(status)}")
                # Take the page back. Left alone it is a tab still telling somebody to
                # reinstall software that is working, which is exactly how an unnecessary
                # reinstall happens. Never opens a window - it only rewrites a page that is
                # already there, and lets it reload into the good news by itself.
                try:
                    healthcheck.clear_alert(repaired=repaired_itself)
                except Exception as exc:
                    log(f"could not clear the alert: {exc}")
                repaired_itself = False
            else:
                log("!" * 62)
                log(healthcheck.describe(status))
                log("The tracker puts this back by itself once the game is closed."
                    if mode == healthcheck.PENDING else
                    "Run the installer again - nothing is being recorded until you do.")
                log("!" * 62)
                try:
                    path = healthcheck.write_alert(status, mode=mode)
                    # One window per breakage. A page already on screen becomes the new text
                    # on its own, and a second popup about the same fault is only shouting
                    # twice.
                    if not quiet and alert_mode is None:
                        webbrowser.open(pathlib.Path(path).as_uri())
                except Exception as exc:
                    log(f"could not raise the alert: {exc}")
            was_seen = True
            alert_mode = mode
        time.sleep(HEALTH_EVERY)


def handoff_in_progress():
    """Is a game update being handed over to the relauncher right now?"""
    try:
        return time.time() - os.path.getmtime(HANDOFF) < HANDOFF_GRACE
    except OSError:
        return False


def _ingest():
    ok, out = run("ingest/ingest.py")
    if not ok:
        log(f"ingest failed:\n{out.strip()}")
        return False
    for line in out.strip().splitlines():
        if line.strip():
            log(f"  {line.strip()}")
    return True


def _submit():
    """Send finished matches to the leaderboard.

    **This can never fail anything.** It runs in its own process, every failure path inside
    analysis/submit.py already returns rather than raises, and the whole call is wrapped
    here as well.
    """
    try:
        ok, out = run("analysis/submit.py")
    except Exception as exc:                       # subprocess itself failing to start
        log(f"  submit skipped: {exc}")
        return
    for line in out.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # "Sent 0, unchanged 60, failed 0" after every match would be noise; only say
        # something when something moved.
        if line.startswith("Sent 0,"):
            continue
        log(f"  {line}")


def catch_up():
    """Ingest and send whatever accumulated while the watcher was not running.

    Runs at startup, which is also what backfills a whole history on a fresh install: the
    submitter batches by bytes and remembers what it has sent, so this is safe to do every
    launch however large the log has grown.
    """
    log("catching up on anything recorded while the watcher was off")
    if _ingest():
        _submit()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval", type=float, default=3.0, help="poll seconds (default 3)")
    ap.add_argument("--log", help="write output to this file (required under pythonw)")
    ap.add_argument("--quiet", action="store_true",
                    help="never open a browser, not even for a broken install")
    ap.add_argument("--no-submit", action="store_true",
                    help="ingest but do not send (for testing)")
    args = ap.parse_args()

    if args.log:
        open_log(args.log)      # must precede any print()

    log("=" * 62)
    log("cbrew Tracker watching for matches")
    log(f"  events   : {EVENTS}")
    log(f"  submit   : {'off' if args.no_submit else 'on'}")
    log("  renders  : nothing. This client has no interface and no web server.")
    log("=" * 62)

    threading.Thread(target=health_watch, args=(args.quiet,), daemon=True).start()

    if not args.no_submit:
        catch_up()
    else:
        _ingest()

    offset = os.path.getsize(EVENTS) if os.path.exists(EVENTS) else 0

    while True:
        try:
            events, offset = new_events(EVENTS, offset)

            # **Both ends of a match, not just the finish.** This client renders nothing,
            # but the site the matches are sent to does, and a match that only arrives when
            # it is over cannot be looked at while it is being played.
            #
            # `ingest.py` writes a row from a start with no end, and refreshes it with
            # COALESCE so the end fills blanks rather than overwriting; `submit.py` sends
            # every row in the projection; the server accepts a match carrying nothing but
            # an id. The in-progress row is simply re-sent when it finishes, which is the
            # ordinary resubmission path.
            #
            # The key is `event_type`, which is what the mod writes and ingest.py reads.
            started = [e for e in events if e.get("event_type") == "match_start"]
            ended = [e for e in events if e.get("event_type") == "match_end"]

            if started or ended:
                # One ingest and one send covers both, for the short match whose start and
                # end land in the same poll. The end is reported in preference to the start
                # because it is the more informative of the two.
                if ended:
                    log("match finished")
                else:
                    last = started[-1]
                    log(f"match started vs {last.get('opponent_display_name', '?')}"
                        f" ({last.get('gameplay_type', '?')})")
                if _ingest() and not args.no_submit:
                    _submit()
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log("stopped")
            return
        except Exception as exc:
            # A bug in here must not end the watch. Recording continues regardless - the
            # mod writes events.jsonl on its own - but a dead watcher means nothing is ever
            # sent, and that would be silent.
            log(f"watch loop error: {exc}")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
