#!/usr/bin/env python3
"""Put the tracker back into an updated game, then open the game (Windows).

    pythonw scripts/relaunch.py            what Pokemon's updater runs, via the mod's hand-off
    python  scripts/relaunch.py --check    the installer's proof that this can run at all

A Pokemon TCG Live update removes the tracker: the updater copies the new version over the
game, both Unity manifests included, and then starts the game the moment Play is clicked.
Started that way, the game records nothing until it is next restarted.

So the mod (mod/CbrewTracker/Hooks/UpdaterHandoffHook.cs) changes what the updater is
told to start. Instead of the game it starts this, through the interpreter and script named
in ~/.ptcgl-tracker/relauncher.txt, and this puts the tracker back and *then* starts the
game. The updated game's first launch records. The macOS relauncher does the same job as a
small app bundle built by install-watcher-macos.sh; this is its Windows counterpart.

**It opens the game whatever else happens.** A repair that fails, a repair that hangs, a
bug in here - every path ends with the game starting. An update that finishes and leaves
the game closed would be far worse than one that opens the game without the tracker, which
the watcher then fixes once the game is closed.

Nothing is shown. This runs under pythonw, which has no console and no stdout, so it keeps
its own log at ~/.ptcgl-tracker/relauncher.log.
"""

import json
import os
import subprocess
import sys
import threading
import time

# By path, not by trusting sys.path[0]: the embeddable Python the installer can fetch runs
# with a ._pth file, and that leaves a script's own folder off sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import modinstall  # noqa: E402

DATA = modinstall.DATA
LOG = os.path.join(DATA, "relauncher.log")
HANDOFF = os.path.join(DATA, "handoff.json")

# The updater starts this and only then quits, so for a moment it is still running from
# inside the install - which the repair treats as the game being open. A minute is far more
# than it needs.
UPDATER_WAIT = 60

# A repair that has not finished in this long is not waited on any further: the game opens
# and the repair is left to finish. It is never killed - a manifest half-written by an
# interrupted repair stops the game starting at all.
REPAIR_LIMIT = 180

# One long-lived file, trimmed rather than left to grow.
LOG_LIMIT = 262144


def log(msg):
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(LOG, "a", encoding="utf-8", errors="replace") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def find_game():
    """(the game's exe, where that answer came from), or (None, None).

    Most specific first: what the updater itself was about to start, which the mod writes
    down at hand-off, then the install receipt.
    """
    try:
        with open(HANDOFF, encoding="utf-8-sig") as fh:
            exe = (json.load(fh) or {}).get("launch_app_at")
        if exe and os.path.isfile(exe):
            return exe, "the updater's hand-off"
    except (OSError, ValueError, AttributeError):
        pass
    try:
        with open(modinstall.RECEIPT, encoding="utf-8-sig") as fh:
            game_data = (json.load(fh) or {}).get("game_data")
        exe = modinstall.game_binary(game_data) if game_data else None
        if exe and os.path.isfile(exe):
            return exe, "the install receipt"
    except (OSError, ValueError, AttributeError):
        pass
    return None, None


def start(exe):
    """Start the game from its own folder, the way the updater would have.

    **The folder matters more than it looks.** The game tells its updater to install into
    the game's *current directory* - --installPath is Directory.GetCurrentDirectory() - so a
    game started from anywhere else would have its next update copied there instead of over
    itself. The updater's own launch inherits the right folder by accident; this sets it on
    purpose.

    ShellExecute, as the updater uses (Process.Start, which defaults to it): it honours what
    the player may have set on the exe, such as "run as administrator", where a bare
    CreateProcess refuses. Python before 3.10 cannot hand ShellExecute a folder, so there,
    and if ShellExecute fails, it falls back to CreateProcess.
    """
    folder = os.path.dirname(exe)
    try:
        os.startfile(exe, cwd=folder)
        return "ShellExecute"
    except TypeError:
        pass                                    # Python < 3.10
    except OSError as exc:
        log("ShellExecute could not start the game (%s) - trying CreateProcess" % exc)
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen([exe], cwd=folder, close_fds=True, creationflags=flags)
    return "CreateProcess"


class Game:
    """Starts the game exactly once, from whichever thread gets there first.

    Two can: the repair starts it while still holding the install lock (so the watcher,
    if it was waiting on that lock, finds the game running and leaves it alone), and the
    main thread starts it as the last thing it does whatever happened.
    """

    def __init__(self):
        self.exe = None
        self._lock = threading.Lock()
        self._started = False

    def start(self):
        with self._lock:
            if self._started:
                return
            self._started = True
        if not self.exe:
            log("could not find the game to start it - start it yourself")
            return
        try:
            how = start(self.exe)
            log("started %s (%s, in %s)" % (self.exe, how, os.path.dirname(self.exe)))
        except Exception as exc:
            log("could not start the game at all (%s) - start it yourself" % exc)


def repair(game):
    """Put the tracker back, starting the game once it is in. Never raises."""
    outcome = {}

    def work():
        try:
            outcome["changed"] = modinstall.repair(log=log, wait=UPDATER_WAIT,
                                                   launch=game.start)
        except modinstall.RepairSkipped as why:
            outcome["skipped"] = why
        except Exception as exc:
            outcome["failed"] = exc

    # Not a daemon, for the same reason it is never killed: if the time limit passes, the
    # game opens and this process stays alive until the repair has finished writing.
    worker = threading.Thread(target=work, name="repair")
    worker.start()
    worker.join(REPAIR_LIMIT)
    if worker.is_alive():
        log("repair still running after %d seconds - starting the game anyway"
            % REPAIR_LIMIT)
    elif "changed" in outcome:
        log("tracker is back in the game")
    elif "skipped" in outcome:
        log("repair skipped (%s) - the watcher will put it back once the game is closed"
            % outcome["skipped"])
    else:
        log("repair failed (%s) - the watcher will put it back once the game is closed"
            % outcome.get("failed"))


def check():
    """For the installer: can this interpreter run this script, and start a game with it?"""
    if os.name != "nt":
        print("relaunch.py is the Windows relauncher; macOS has its own")
        return 1
    print("relauncher ok: Python %s, modinstall from %s"
          % (sys.version.split()[0], os.path.dirname(os.path.abspath(modinstall.__file__))))
    return 0


def main(argv):
    if "--check" in argv:
        return check()

    try:
        if os.path.getsize(LOG) > LOG_LIMIT:
            open(LOG, "w").close()
    except OSError:
        pass
    log("=" * 54)
    log("Pokemon's updater handed over - putting the tracker back before the game opens")

    game = Game()
    try:
        game.exe, source = find_game()
        log("game: %s" % ("%s (from %s)" % (game.exe, source) if game.exe
                          else "not found - nothing to start afterwards"))
        repair(game)
    except BaseException as exc:                # including KeyboardInterrupt / SystemExit
        log("relauncher error: %r" % (exc,))
    finally:
        try:
            os.remove(HANDOFF)
        except OSError:
            pass
        game.start()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
