#!/usr/bin/env python3
"""Put the tracker into the game, and put it back when a game update takes it out.

    python scripts/modinstall.py --stash mod/build       # keep a copy for later repairs
    python scripts/modinstall.py --apply <game_data>     # copy + register (the install)
    python scripts/modinstall.py --repair                # apply from the stash, if needed

---------------------------------------------------------------------------------------
Why this exists
---------------------------------------------------------------------------------------
**A Pokemon TCG Live update removes the tracker every time.** The update replaces the whole
app, so the two assemblies go from `Managed\\` and both Unity manifests lose their entry.
The game then runs perfectly normally and records nothing, which is the worst shape a
failure can take - there is nothing to notice.

`healthcheck.py` detects that within two minutes, and this module repairs it, so nobody has
to find the installer they downloaded months ago.

---------------------------------------------------------------------------------------
The stash is what makes repair possible at all
---------------------------------------------------------------------------------------
The folder a person unzipped looks like leftovers and gets deleted. So the install also
stashes the assemblies under `~/.ptcgl-tracker/mod/`, beside the receipt, and a repair needs
nothing but this machine: no download, no installer, no network.

---------------------------------------------------------------------------------------
One copy of the registration logic
---------------------------------------------------------------------------------------
The installers, the watcher's repair and the update relauncher all call this module. A fresh
install and a repaired install have to produce byte-identical manifests, or the repaired one
is subtly a different install, so there is exactly one implementation.
"""

import argparse
import contextlib
import datetime
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time

DATA = os.path.expanduser("~/.ptcgl-tracker")
STASH = os.path.join(DATA, "mod")
LOCK = os.path.join(DATA, "modinstall.lock")
RECEIPT = os.path.join(DATA, "install.json")

ASSEMBLIES = ("CbrewTracker.dll", "0Harmony.dll")

# Type 16 is what the game's own (non-Unity) assemblies use in ScriptingAssemblies.json.
SCRIPTING_TYPE = 16

# Names other builds of this tracker have registered under. Any of them found in the game
# is removed before this one goes in - see foreign_installs().
FOREIGN_PREFIXES = ("PtcglTracker", "CbrewTracker")

# loadTypes 1 = BeforeSceneLoad, matching the game's own Firebase entry.
RUNTIME_ENTRY = {
    "assemblyName": "CbrewTracker",
    "nameSpace": "CbrewTracker",
    "className": "Bootstrap",
    "methodName": "Init",
    "loadTypes": 1,
    "isUnityClass": False,
}


# ---------------------------------------------------------------------------------------
# The stash
# ---------------------------------------------------------------------------------------
def stash_from(build_dir):
    """Keep a copy of the assemblies for later repairs. Returns the files copied.

    Anything else in the stash is removed first, so the stash only ever holds the build
    that is installed - a repair cannot put back an assembly this install replaced.
    """
    os.makedirs(STASH, exist_ok=True)
    for name in os.listdir(STASH):
        path = os.path.join(STASH, name)
        if name not in ASSEMBLIES and os.path.isfile(path):
            os.remove(path)
    copied = []
    for name in ASSEMBLIES:
        src = os.path.join(build_dir, name)
        if not os.path.isfile(src):
            raise FileNotFoundError("not in the build: %s" % src)
        shutil.copy2(src, os.path.join(STASH, name))
        copied.append(name)
    return copied


def stash_ready():
    """Is there a complete set of assemblies to repair from?"""
    return all(os.path.isfile(os.path.join(STASH, n)) for n in ASSEMBLIES)


# ---------------------------------------------------------------------------------------
# Where the game keeps itself, on either platform
# ---------------------------------------------------------------------------------------
# Unity lays a player out in two shapes, and everything below is *derived* from `game_data`
# rather than hard-coded. That is deliberate: the game's marketing name has an accented
# character which has caused trouble elsewhere in this project, and no path here needs it.
#
#   Windows   <game>\\<Name>_Data\\                   binary at <game>\\<Name>.exe
#   macOS     <App>.app/Contents/Resources/Data/     binary at <App>.app/Contents/MacOS/<exec>
#
# The macOS half is the awkward one. `Data` is a generic folder name three levels inside a
# bundle, so recognising the layout means checking the whole tail of the path - the last
# segment alone tells you nothing.


def app_bundle(game_data):
    """The .app `game_data` lives inside, or None when this is not a macOS layout."""
    resources = os.path.dirname(game_data)
    contents = os.path.dirname(resources)
    bundle = os.path.dirname(contents)
    if (os.path.basename(game_data) == "Data"
            and os.path.basename(resources) == "Resources"
            and os.path.basename(contents) == "Contents"
            and bundle.endswith(".app")):
        return bundle
    return None


def game_binary(game_data):
    """The executable this install runs, or None if the layout is unrecognised."""
    bundle = app_bundle(game_data)
    if bundle:
        # CFBundleExecutable, not "the only file in MacOS/": a bundle may carry helper
        # executables, and the plist is the authoritative answer to which one is the game.
        try:
            with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as fh:
                name = plistlib.load(fh).get("CFBundleExecutable")
        except (OSError, ValueError):
            return None
        if not name:
            return None
        return os.path.join(bundle, "Contents", "MacOS", name)

    base = os.path.basename(game_data)
    if base.endswith("_Data"):
        return os.path.join(os.path.dirname(game_data), base[: -len("_Data")] + ".exe")
    return None


# ---------------------------------------------------------------------------------------
# Is the game running?
# ---------------------------------------------------------------------------------------
def game_running(game_data):
    """True if the game looks like it is running, and True when we cannot tell.

    **Failing closed is the whole point.** Writing into the game's assemblies under a
    running game is how you corrupt an install or get the copy silently reverted at exit,
    so anything short of a confident "no" has to mean "do not touch it".

    On Windows the running .exe is held open by the OS, so opening it for write raises
    PermissionError. That is a fact about the file rather than a guess about a process
    list, and it needs no subprocess. macOS holds no such lock, so it asks the process
    table instead - by full path, because the game's own name is far too generic to match
    on safely.

    **"The game" includes its updater, on both platforms.** Pokemon's updater lives inside
    the install - Contents/Resources/Updater/<version>/Updater.app on macOS,
    <game>\\Updater\\<version>\\Updater.exe on Windows - and it spends its whole run copying
    the new version over the old one. Matching only the game binary let a two-minute health
    pass that happened to land mid-update go ahead and write into the same files the
    updater was writing - two programs, one install, no coordination. On Windows the game
    has quit by then, so its exe is unlocked and looked like a green light.
    """
    binary = game_binary(game_data)
    if not binary:
        return True                     # unrecognised layout: do not risk it

    if os.name == "nt":
        # The updaters first. Mid-update the updater may be rewriting the game's own exe,
        # and a probe that opens it for write at that instant could be what makes its copy
        # fail - so while an updater is running the game's exe is not touched at all.
        install = os.path.dirname(game_data)
        updaters = os.path.join(install, "Updater")
        try:
            versions = os.listdir(updaters)
        except OSError:
            versions = []               # no updater folder: nothing of it can be running
        for version in versions:
            updater = os.path.join(updaters, version, "Updater.exe")
            if os.path.isfile(updater) and _locked(updater):
                return True
        if not os.path.exists(binary):
            # The exe is gone entirely - a repair cannot make that worse, and the caller's
            # own checks will fail on the missing game folder first.
            return False
        return _locked(binary)

    # Matched on what each process is *executing*, never on its arguments. `ps -o comm`
    # gives the full path of the executable on macOS, so a process counts only if it is
    # actually running code from inside the bundle. `pgrep -f` would search every argument
    # of every process, and so treat anything that merely mentions the game's path as the
    # game itself: a `tail` on one of its logs, a script polling for it. The bare process
    # name would be worse still: it is three ordinary words.
    bundle = app_bundle(game_data)
    inside = (bundle + "/Contents/") if bundle else None
    try:
        out = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True,
                             timeout=10)
        if out.returncode != 0:
            return True                 # could not tell
        executables = [line.strip() for line in out.stdout.splitlines() if line.strip()]
        if not executables:
            return True                 # an empty process table is not a confident no
        for exe in executables:
            if (exe.startswith(inside) if inside else exe == binary):
                return True
        return False
    except Exception:
        return True


def _locked(path):
    """Is this Windows executable running? True when we cannot tell.

    A running image cannot be opened for write, so a PermissionError is the answer. Any
    other failure to open is not a confident "no", and is treated the same way.
    """
    try:
        with open(path, "r+b"):
            return False                # opened for write: not running
    except OSError:
        return True


@contextlib.contextmanager
def _exclusive():
    """One writer into the game at a time, across processes.

    Two things can repair the same install at once: the watcher's two-minute pass, and
    the relauncher Pokemon's updater opens at the end of an update. Each is fine alone and
    harmless twice - every step is idempotent - except the re-sign, where two codesign runs
    rewriting one bundle's signature at the same moment can leave it describing neither.
    An advisory lock beside the receipt serialises them.

    Windows has no re-sign, but needs the lock just as much: there, a file one writer has
    open is a file the other cannot replace, so two repairs
    at once fail rather than merely repeat - and an assembly being copied in at the moment
    the game opens it is an assembly the game does not load. msvcrt's byte lock is the same
    thing as flock for this purpose, and is released by the OS if the holder dies.
    """
    os.makedirs(DATA, exist_ok=True)
    if os.name == "nt":
        import msvcrt
        # "a", not "w": opening for write truncates, and truncating a file another process
        # holds a lock in is refused on Windows. The lock is on byte 0, which may lie past
        # the end of an empty file - Windows allows that.
        with open(LOCK, "a") as fh:
            fh.seek(0)
            while True:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.2)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    with open(LOCK, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------------------
# The install itself
# ---------------------------------------------------------------------------------------
def copy_assemblies(game_data, source):
    """Copy the two assemblies into the game's Managed folder. Returns what changed."""
    managed = os.path.join(game_data, "Managed")
    if not os.path.isdir(managed):
        raise NotADirectoryError("no Managed folder in %s" % game_data)
    done = []
    for name in ASSEMBLIES:
        src = os.path.join(source, name)
        if not os.path.isfile(src):
            raise FileNotFoundError("missing from %s: %s" % (source, name))
        shutil.copy2(src, os.path.join(managed, name))
        done.append(name)
    return done


def register(game_data):
    """Add the tracker to both Unity manifests. Idempotent. Returns what changed.

    Both files are plain JSON the install appends one entry to. Written through a temp file
    and os.replace so a crash mid-write cannot leave the game with a truncated manifest -
    which would stop the game itself from starting, not just the tracker.
    """
    changed = []

    sa_path = os.path.join(game_data, "ScriptingAssemblies.json")
    with open(sa_path, encoding="utf-8") as fh:
        sa = json.load(fh)
    # names[] and types[] are parallel arrays and must stay the same length.
    dirty = False
    for dll in ASSEMBLIES:
        if dll not in sa.get("names", []):
            sa.setdefault("names", []).append(dll)
            sa.setdefault("types", []).append(SCRIPTING_TYPE)
            changed.append("%s -> ScriptingAssemblies" % dll)
            dirty = True
    if dirty:
        _write_json(sa_path, sa)

    ri_path = os.path.join(game_data, "RuntimeInitializeOnLoads.json")
    with open(ri_path, encoding="utf-8") as fh:
        ri = json.load(fh)
    root = ri.setdefault("root", [])
    if not any(e.get("assemblyName") == "CbrewTracker" for e in root):
        root.append(dict(RUNTIME_ENTRY))
        _write_json(ri_path, ri)
        changed.append("Bootstrap.Init -> RuntimeInitializeOnLoads")

    return changed


def _write_json(path, obj):
    tmp = path + ".ptcgl-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------------------
# Putting the seal back (macOS)
# ---------------------------------------------------------------------------------------
def reseal(game_data, log=lambda _m: None):
    """Re-sign the .app after changing what is inside it. Does nothing off macOS.

    A macOS application is not a folder that happens to hold a program - it is a *sealed*
    one. `Contents/_CodeSignature/CodeResources` records a hash of every file in the
    bundle, the game's assemblies included, so the moment two DLLs are copied into
    `Managed/` the seal no longer describes what is on disk.

    So the install re-signs the bundle ad-hoc, which is what the sign `-` means: a valid
    signature with no developer identity behind it. That is the most a tool running on the
    player's own machine can produce, and it is enough - the bundle describes itself
    honestly again.

    **Entitlements are carried across, and a failure to carry them is fatal.** They are not
    decoration: this game ships with `com.apple.security.cs.disable-executable-page-
    protection`, without which Mono cannot make its JIT pages executable on Apple Silicon
    and the game dies at startup. `codesign` drops entitlements on a re-sign unless they
    are handed back to it, so dropping them silently would trade "not recording" for "does
    not launch" - much the worse failure, and one nobody would connect to a tracker.
    """
    bundle = app_bundle(game_data)
    if bundle is None:
        return None                     # Windows, or a layout with no bundle to seal

    log("re-sealing %s" % os.path.basename(bundle))

    # Read the entitlements off the copy we are about to replace. `:-` asks for the raw
    # plist on stdout; without the colon codesign pretty-prints it for a human instead,
    # which is not something plistlib can read back.
    dumped = subprocess.run(["codesign", "-d", "--entitlements", ":-", bundle],
                            capture_output=True, timeout=120)
    entitlements = None
    if dumped.returncode == 0 and dumped.stdout.strip():
        try:
            entitlements = plistlib.loads(dumped.stdout)
        except Exception as exc:
            raise RuntimeError(
                "the game's entitlements could not be read (%s), and re-signing without "
                "them would stop the game launching at all. Nothing was re-signed." % exc)

    argv = ["codesign", "--force", "--sign", "-"]
    tmp = None
    if entitlements:
        tmp = os.path.join(DATA, "entitlements.plist")
        os.makedirs(DATA, exist_ok=True)
        with open(tmp, "wb") as fh:
            plistlib.dump(entitlements, fh)
        argv += ["--entitlements", tmp]
        log("  preserving %d entitlement(s)" % len(entitlements))
    argv.append(bundle)

    try:
        signed = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass

    if signed.returncode != 0:
        raise RuntimeError("codesign could not re-sign the game: %s"
                           % (signed.stderr or signed.stdout or "").strip())

    # Verify rather than trust. A signature that did not take is exactly the kind of thing
    # that shows up later as a game which will not start, with nothing pointing back here.
    checked = subprocess.run(["codesign", "--verify", "--strict", bundle],
                             capture_output=True, text=True, timeout=600)
    if checked.returncode != 0:
        raise RuntimeError("the game was re-signed but the signature does not verify: %s"
                           % (checked.stderr or checked.stdout or "").strip())
    log("  signature verified")
    return bundle


# ---------------------------------------------------------------------------------------
# Taking an install back out
# ---------------------------------------------------------------------------------------
def unregister(game_data, names):
    """Drop `names` from both Unity manifests. Idempotent. Returns what changed.

    `names` are bare assembly names ("CbrewTracker"); the .dll is added where the
    manifest wants it. Surgical removal, not a restore from backup: the install only ever
    *appends* its entries, so deleting exactly those returns the manifest to what it was -
    and unlike copying a saved file back, it cannot clobber a manifest that has changed
    since for some reason of the game's own.
    """
    changed = []
    dlls = {n + ".dll" for n in names}

    sa_path = os.path.join(game_data, "ScriptingAssemblies.json")
    try:
        with open(sa_path, encoding="utf-8") as fh:
            sa = json.load(fh)
    except (OSError, ValueError):
        sa = None
    if sa:
        # names[] and types[] are parallel arrays, so drop by index or they fall out of
        # step and the game stops loading assemblies that have nothing to do with us.
        keep = [(n, t) for n, t in zip(sa.get("names", []), sa.get("types", []))
                if n not in dlls]
        if len(keep) != len(sa.get("names", [])):
            sa["names"] = [n for n, _ in keep]
            sa["types"] = [t for _, t in keep]
            _write_json(sa_path, sa)
            changed.append("removed %s from ScriptingAssemblies"
                           % ", ".join(sorted(dlls)))

    ri_path = os.path.join(game_data, "RuntimeInitializeOnLoads.json")
    try:
        with open(ri_path, encoding="utf-8") as fh:
            ri = json.load(fh)
    except (OSError, ValueError):
        ri = None
    if ri:
        root = ri.get("root", [])
        keep = [e for e in root if e.get("assemblyName") not in names]
        if len(keep) != len(root):
            ri["root"] = keep
            _write_json(ri_path, ri)
            changed.append("removed %s from RuntimeInitializeOnLoads"
                           % ", ".join(sorted(names)))

    return changed


def foreign_installs(game_data):
    """Other builds of this tracker already registered in the game, by bare name.

    Any assembly under one of FOREIGN_PREFIXES that is not this one. Two builds patch the
    same methods and append to the same `events.jsonl`, so a machine carrying both records
    every match twice and leaves two Harmony patches fighting over one method. They must
    never be installed side by side.

    The installer is the only thing in a position to notice, because from the *game's*
    point of view nothing is wrong: two assemblies load, both work, and the damage shows up
    later as a ladder history with every match in it twice.
    """
    ours = set(n[: -len(".dll")] for n in ASSEMBLIES)
    found = set()

    try:
        with open(os.path.join(game_data, "ScriptingAssemblies.json"), encoding="utf-8") as fh:
            for name in json.load(fh).get("names", []):
                if name.startswith(FOREIGN_PREFIXES) and name.endswith(".dll"):
                    base = name[: -len(".dll")]
                    if base not in ours:
                        found.add(base)
    except (OSError, ValueError):
        pass

    try:
        with open(os.path.join(game_data, "RuntimeInitializeOnLoads.json"), encoding="utf-8") as fh:
            for entry in json.load(fh).get("root", []):
                name = entry.get("assemblyName") or ""
                if name.startswith(FOREIGN_PREFIXES) and name not in ours:
                    found.add(name)
    except (OSError, ValueError):
        pass

    return sorted(found)


def remove(game_data, names, log=lambda _m: None):
    """Delete `names` from the game and unregister them, then put the seal back."""
    with _exclusive():
        changed = []
        managed = os.path.join(game_data, "Managed")
        for name in names:
            path = os.path.join(managed, name + ".dll")
            if os.path.isfile(path):
                os.remove(path)
                changed.append("deleted %s.dll" % name)
        changed += unregister(game_data, names)
        if changed:
            for line in changed:
                log("  %s" % line)
            reseal(game_data, log=log)
        return changed


def apply(game_data, source=None, log=lambda _m: None):
    """Copy the assemblies in, register them, and re-seal the bundle on macOS.

    The re-seal belongs *here*, not in the installer, for the same reason the manifest
    edits do: the watcher's unattended repair calls this too, and a repair that left the
    bundle unsealed would fix the recording and break the launching.
    """
    with _exclusive():
        return _apply(game_data, source, log)


def _apply(game_data, source=None, log=lambda _m: None):
    """apply(), for a caller already holding the lock."""
    source = source or STASH
    changed = copy_assemblies(game_data, source) + register(game_data)
    # Unconditionally, not `if changed`: a bundle whose files are all correct can still
    # have a stale seal - that is exactly what a repair of a signature-only problem
    # looks like - and re-signing an already-valid bundle costs a fraction of a second.
    if reseal(game_data, log=log):
        changed.append("the application bundle seal")
    return changed


# ---------------------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------------------
class RepairSkipped(Exception):
    """Repair was not attempted, and why. Not a failure - a reason to wait."""


class RepairDeferred(RepairSkipped):
    """Repair will happen by itself, as soon as something outside our control changes.

    Only the running game raises this: nobody has to do anything except close it, and the
    next pass finds it closed and repairs it. Every other skip needs a person to act, and
    telling somebody to go and find the installer when the tracker is about to put itself
    back is how you get an unnecessary reinstall - so the caller has to be able to tell the
    two apart.
    """


def repair(game_data=None, log=lambda _m: None, wait=0, launch=None):
    """Put the install back from the stash. Raises RepairSkipped when it should not run.

    Every guard here answers "could this make things worse than leaving them broken?" - a
    repair that half-writes a manifest under a running game is a worse outcome than a
    tracker that is merely not recording.

    `wait` is for the relauncher. Pokemon's updater opens it and only *then* quits, so for
    a second or two after the hand-off the updater is still a process inside the install.
    Waiting it out is right there; for the watcher, which will simply look again in two
    minutes, the default of not waiting is.

    **"Is the game closed?" is asked again under the lock, right before writing.** The
    watcher and the relauncher can both arrive at a repair together, and whichever waited
    on the lock has to look again once it has it - by then the other may have repaired the
    install and started the game. That is also what `launch` is for: the relauncher's way of
    starting the game *before* the lock is let go, so that whoever was waiting finds the game
    running and stands back, rather than copying assemblies in while the game loads them.
    """
    if game_data is None:
        try:
            with open(RECEIPT, encoding="utf-8-sig") as fh:
                game_data = (json.load(fh) or {}).get("game_data")
        except (OSError, ValueError):
            game_data = None
    if not game_data:
        raise RepairSkipped("no install receipt - the tracker has never been installed here")
    if not os.path.isdir(game_data):
        # The game moved or was reinstalled elsewhere. Finding it again is the installer's
        # job (it has the search paths); guessing here would risk writing into the wrong
        # copy of the game.
        raise RepairSkipped("the game is no longer at %s - it was moved or reinstalled"
                            % game_data)
    if not stash_ready():
        raise RepairSkipped("no stashed copy of the tracker to repair from - run the "
                            "installer once more")
    deadline = time.monotonic() + max(0, wait)
    while True:
        with _exclusive():
            if not game_running(game_data):
                log("repairing the install at %s" % game_data)
                changed = _apply(game_data, log=log)
                for line in changed:
                    log("  restored %s" % line)
                stamp_repair()
                if launch:
                    launch()
                return changed
        # Slept on outside the lock, so a wait of ours never holds up the other writer.
        if time.monotonic() >= deadline:
            raise RepairDeferred("the game or its updater is running - will repair once "
                                 "it is closed")
        time.sleep(1)


def stamp_repair(when=None):
    """Record when the install was last put back. Best effort - never fails a repair.

    The health check needs this to know what its evidence is worth. The mod truncates its
    log at every launch, so after a game update the newest log is the one from *before* the
    update, still saying every hook attached - and a repair that copies the files back would
    otherwise be confirmed by a log written before the breakage it just repaired. Against
    this stamp that log is visibly too old to mean anything, and the answer becomes "not
    confirmed until the game is next started", which is the truth.
    """
    when = when or datetime.datetime.now(datetime.timezone.utc)
    try:
        with open(RECEIPT, encoding="utf-8-sig") as fh:
            receipt = json.load(fh) or {}
        receipt["repaired_at"] = when.strftime("%Y-%m-%dT%H:%M:%SZ")
        tmp = RECEIPT + ".ptcgl-tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(receipt, fh, indent=4)
        os.replace(tmp, RECEIPT)
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Install or repair the tracker's assemblies.")
    ap.add_argument("--stash", metavar="BUILD_DIR",
                    help="keep a copy of the built assemblies for later repairs")
    ap.add_argument("--apply", metavar="GAME_DATA",
                    help="copy the assemblies in and register them")
    ap.add_argument("--source", metavar="DIR",
                    help="where to copy from (default: the stash)")
    ap.add_argument("--repair", action="store_true",
                    help="restore the install from the stash if it is broken")
    ap.add_argument("--wait", metavar="SECONDS", type=float, default=0,
                    help="with --repair: wait this long for the game and its updater to exit")
    ap.add_argument("--foreign", metavar="GAME_DATA",
                    help="list other builds of this tracker registered in the game")
    ap.add_argument("--remove", metavar="GAME_DATA",
                    help="take assemblies back out of the game and re-seal it")
    ap.add_argument("--names", metavar="NAME", nargs="+", default=list(
                        n[: -len(".dll")] for n in ASSEMBLIES),
                    help="which assemblies --remove acts on (default: this tracker's)")
    args = ap.parse_args()

    if args.stash:
        for name in stash_from(args.stash):
            print("    stashed %s" % name)

    if args.apply:
        for line in apply(args.apply, args.source, log=lambda m: print("    %s" % m)):
            print("    %s" % line)

    if args.foreign:
        for name in foreign_installs(args.foreign):
            print(name)

    if args.remove:
        removed = remove(args.remove, args.names, log=lambda m: print("    %s" % m))
        if not removed:
            print("    nothing of %s was in the game" % ", ".join(args.names))

    if args.repair:
        try:
            changed = repair(log=lambda m: print("    %s" % m), wait=args.wait)
        except RepairSkipped as why:
            print("    not repaired: %s" % why)
            return 2
        if not changed:
            print("    nothing needed repairing")

    if not (args.stash or args.apply or args.repair or args.remove
            or args.foreign):
        ap.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
