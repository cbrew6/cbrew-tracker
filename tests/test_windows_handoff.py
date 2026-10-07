"""Tests for the Windows side of a game update: the relauncher, and repair around it.

    python -m unittest discover -s tests -v

Windows only. Everything happens in a throwaway folder - a fake game install and a fake
~/.ptcgl-tracker - and never touches the real game or the real data folder.

"Running" is real, not mocked: the fake game and the fake updater are copies of PING.EXE,
which runs from anywhere and holds its own exe locked while it does - exactly the fact
modinstall.game_running() reads. Starting the game afterwards is the one thing mocked here;
that half is proved against Pokemon's real updater, outside this suite.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

if os.name == "nt":
    import msvcrt

    import modinstall
    import relaunch

PING = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "PING.EXE")

# A console program started with no window of its own: nothing flashes up while this runs.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _exe_locked(path):
    """The test's own probe, independent of the code under test."""
    try:
        with open(path, "r+b"):
            return False
    except OSError:
        return True


@unittest.skipUnless(os.name == "nt", "the Windows relauncher")
class Base(unittest.TestCase):
    """A fake install and a fake data folder, both under paths with spaces and accents."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ptcgl-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.home = os.path.join(self.tmp, "Jöhn Smith")
        self.data_dir = os.path.join(self.home, ".ptcgl-tracker")
        self.game = os.path.join(self.home, "The Pokémon Company International",
                                 "Pokémon Trading Card Game Live")
        self.exe = os.path.join(self.game, "Pokemon TCG Live.exe")
        self.game_data = os.path.join(self.game, "Pokemon TCG Live_Data")
        self.updater = os.path.join(self.game, "Updater", "1.5.0", "Updater.exe")

        os.makedirs(os.path.join(self.game_data, "Managed"))
        os.makedirs(os.path.dirname(self.updater))
        shutil.copy(PING, self.exe)
        shutil.copy(PING, self.updater)
        # What a game update leaves behind: the game's own manifests, without the tracker.
        self._write(os.path.join(self.game_data, "ScriptingAssemblies.json"),
                    {"names": ["Assembly-CSharp.dll"], "types": [16]})
        self._write(os.path.join(self.game_data, "RuntimeInitializeOnLoads.json"),
                    {"root": [{"assemblyName": "Assembly-CSharp", "nameSpace": "",
                               "className": "Boot", "methodName": "Init",
                               "loadTypes": 1, "isUnityClass": False}]})

        stash = os.path.join(self.data_dir, "mod")
        os.makedirs(stash)
        for name in modinstall.ASSEMBLIES:
            with open(os.path.join(stash, name), "wb") as fh:
                fh.write(b"MZ fake " + name.encode())
        self._write(os.path.join(self.data_dir, "install.json"),
                    {"game_data": self.game_data, "installed_at": "2026-01-01T00:00:00Z",
                     "platform": "windows"})

        # Point both modules at the fake data folder.
        patches = {
            (modinstall, "DATA"): self.data_dir,
            (modinstall, "STASH"): stash,
            (modinstall, "LOCK"): os.path.join(self.data_dir, "modinstall.lock"),
            (modinstall, "RECEIPT"): os.path.join(self.data_dir, "install.json"),
            (relaunch, "DATA"): self.data_dir,
            (relaunch, "LOG"): os.path.join(self.data_dir, "relauncher.log"),
            (relaunch, "HANDOFF"): os.path.join(self.data_dir, "handoff.json"),
        }
        for (module, name), value in patches.items():
            p = mock.patch.object(module, name, value)
            p.start()
            self.addCleanup(p.stop)

        self.procs = []
        self.addCleanup(self._stop_all)

    # -- helpers -----------------------------------------------------------------------

    @staticmethod
    def _write(path, obj):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    @staticmethod
    def _read(path):
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)

    def run_for(self, exe, seconds):
        """Start a fake process from `exe` that lives about `seconds`, and wait until it
        holds its exe - the moment it counts as running."""
        proc = subprocess.Popen([exe, "-n", str(int(seconds) + 1), "127.0.0.1"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=NO_WINDOW)
        self.procs.append(proc)
        deadline = time.monotonic() + 5
        while not _exe_locked(exe):
            if time.monotonic() > deadline:
                self.fail("fake process never started: %s" % exe)
            time.sleep(0.05)
        return proc

    def _stop_all(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def hold_lock(self, seconds):
        """Hold the install lock from another process, as the watcher or relauncher would.
        Returns once it is held."""
        ready = os.path.join(self.tmp, "lock-held-%d" % len(self.procs))
        code = ("import sys, time; sys.path.insert(0, sys.argv[1]); import modinstall;\n"
                "modinstall.DATA = sys.argv[2]; modinstall.LOCK = sys.argv[3]\n"
                "with modinstall._exclusive():\n"
                "    open(sys.argv[4], 'w').close(); time.sleep(float(sys.argv[5]))\n")
        proc = subprocess.Popen([sys.executable, "-c", code, SCRIPTS, self.data_dir,
                                 modinstall.LOCK, ready, str(seconds)],
                                creationflags=NO_WINDOW)
        self.procs.append(proc)
        deadline = time.monotonic() + 10
        while not os.path.exists(ready):
            if time.monotonic() > deadline:
                self.fail("lock holder never took the lock")
            time.sleep(0.05)
        return proc

    def registered(self):
        names = self._read(os.path.join(self.game_data, "ScriptingAssemblies.json"))["names"]
        root = self._read(os.path.join(self.game_data, "RuntimeInitializeOnLoads.json"))["root"]
        return (all(n in names for n in modinstall.ASSEMBLIES)
                and any(e.get("assemblyName") == "CbrewTracker" for e in root))


class GameRunning(Base):
    """modinstall.game_running(): the game, and now its updater, by the lock on the exe."""

    def test_nothing_running(self):
        self.assertFalse(modinstall.game_running(self.game_data))

    def test_the_game(self):
        self.run_for(self.exe, 10)
        self.assertTrue(modinstall.game_running(self.game_data))

    def test_the_updater_counts_as_the_game(self):
        # Mid-update the game has quit and its exe is unlocked; a health pass landing then
        # must not repair into files the updater is still copying.
        self.run_for(self.updater, 10)
        self.assertTrue(modinstall.game_running(self.game_data))

    def test_any_updater_version(self):
        newer = os.path.join(self.game, "Updater", "1.6.0", "Updater.exe")
        os.makedirs(os.path.dirname(newer))
        shutil.copy(PING, newer)
        self.run_for(newer, 10)
        self.assertTrue(modinstall.game_running(self.game_data))

    def test_closed_again(self):
        proc = self.run_for(self.updater, 10)
        proc.kill()
        proc.wait()
        self.assertFalse(modinstall.game_running(self.game_data))

    def test_no_updater_folder(self):
        shutil.rmtree(os.path.join(self.game, "Updater"))
        self.assertFalse(modinstall.game_running(self.game_data))
        self.run_for(self.exe, 10)
        self.assertTrue(modinstall.game_running(self.game_data))

    def test_a_version_folder_without_an_updater(self):
        os.makedirs(os.path.join(self.game, "Updater", "0.9.0"))
        self.assertFalse(modinstall.game_running(self.game_data))


class Lock(Base):
    """modinstall._exclusive(): one writer at a time, now on Windows too."""

    def test_waits_for_another_process(self):
        self.hold_lock(2)
        started = time.monotonic()
        with modinstall._exclusive():
            waited = time.monotonic() - started
        self.assertGreater(waited, 1.0)

    def test_free_lock_is_immediate(self):
        started = time.monotonic()
        with modinstall._exclusive():
            pass
        self.assertLess(time.monotonic() - started, 1.0)

    def test_released_when_the_holder_dies(self):
        proc = self.hold_lock(60)
        proc.kill()
        proc.wait()
        started = time.monotonic()
        with modinstall._exclusive():
            pass
        self.assertLess(time.monotonic() - started, 2.0)


class Repair(Base):
    """modinstall.repair(): checks and writes under one lock, and can start the game
    before letting go of it."""

    def test_repairs_a_broken_install(self):
        changed = modinstall.repair(log=lambda m: None)
        self.assertTrue(changed)
        self.assertTrue(self.registered())
        for name in modinstall.ASSEMBLIES:
            self.assertTrue(os.path.isfile(os.path.join(self.game_data, "Managed", name)))
        self.assertIn("repaired_at", self._read(modinstall.RECEIPT))

    def test_deferred_while_the_updater_runs(self):
        self.run_for(self.updater, 10)
        with self.assertRaises(modinstall.RepairDeferred):
            modinstall.repair(log=lambda m: None, wait=0)
        self.assertFalse(self.registered())

    def test_waits_out_the_updater_quitting(self):
        # The relauncher's case: the updater starts it and only then quits.
        self.run_for(self.updater, 2)
        started = time.monotonic()
        modinstall.repair(log=lambda m: None, wait=30)
        self.assertGreater(time.monotonic() - started, 1.0)
        self.assertTrue(self.registered())

    def test_looks_again_after_waiting_on_the_lock(self):
        # The race this closes: the watcher saw the game closed, then waited on the lock
        # while the relauncher repaired and started the game. Looking again under the lock,
        # it must find the game running and write nothing.
        self.hold_lock(3)
        # The game starts only once the repair is already waiting on the lock - so the
        # game is closed when it first looks, and running by the time it could write.
        timer = threading.Timer(1.0, self.run_for, (self.exe, 15))
        timer.start()
        self.addCleanup(timer.cancel)
        with mock.patch.object(modinstall, "copy_assemblies") as copy:
            with self.assertRaises(modinstall.RepairDeferred):
                modinstall.repair(log=lambda m: None, wait=0)
            copy.assert_not_called()

    def test_launch_happens_while_the_lock_is_held(self):
        seen = []

        def launch():
            # A second handle on the lock file, in this same process, is refused while the
            # repair holds the byte - which is what the watcher would run into.
            with open(modinstall.LOCK, "a") as fh:
                fh.seek(0)
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    seen.append("free")
                except OSError:
                    seen.append("held")

        modinstall.repair(log=lambda m: None, launch=launch)
        self.assertEqual(seen, ["held"])

    def test_no_launch_when_deferred(self):
        self.run_for(self.exe, 10)
        launch = mock.Mock()
        with self.assertRaises(modinstall.RepairDeferred):
            modinstall.repair(log=lambda m: None, wait=0, launch=launch)
        launch.assert_not_called()


class Relaunch(Base):
    """scripts/relaunch.py: repair, then start the game - whatever happens."""

    def setUp(self):
        super().setUp()
        self.started = []
        p = mock.patch.object(relaunch, "start", side_effect=self._started)
        p.start()
        self.addCleanup(p.stop)

    def _started(self, exe):
        self.started.append(exe)
        return "test"

    def hand_off(self, exe=None):
        self._write(relaunch.HANDOFF, {"launch_app_at": exe or self.exe,
                                       "handed_off_at": "2026-10-06T22:00:00.000Z",
                                       "client_version": "1.42.2"})

    def log_text(self):
        with open(relaunch.LOG, encoding="utf-8") as fh:
            return fh.read()

    def test_the_whole_hand_off(self):
        self.hand_off()
        self.run_for(self.updater, 2)           # still quitting when the relauncher starts
        self.assertEqual(relaunch.main([]), 0)
        self.assertEqual(self.started, [self.exe])
        self.assertTrue(self.registered())
        self.assertFalse(os.path.exists(relaunch.HANDOFF))
        log = self.log_text()
        self.assertIn("from the updater's hand-off", log)
        self.assertIn("tracker is back in the game", log)

    def test_the_game_starts_inside_the_lock(self):
        # Started by the repair, under the lock - not left to the end of main().
        self.hand_off()
        order = []
        real_repair = modinstall.repair

        def spy(*a, **kw):
            launch = kw["launch"]

            def wrapped():
                order.append("start")
                launch()
            kw["launch"] = wrapped
            result = real_repair(*a, **kw)
            order.append("repair returned")
            return result

        with mock.patch.object(modinstall, "repair", side_effect=spy):
            relaunch.main([])
        self.assertEqual(order, ["start", "repair returned"])
        self.assertEqual(len(self.started), 1)

    def test_falls_back_to_the_receipt(self):
        self.assertEqual(relaunch.main([]), 0)
        self.assertEqual(self.started, [self.exe])
        self.assertIn("from the install receipt", self.log_text())

    def test_a_handoff_naming_a_missing_game_falls_back(self):
        self.hand_off(os.path.join(self.tmp, "nowhere.exe"))
        relaunch.main([])
        self.assertEqual(self.started, [self.exe])

    def test_starts_the_game_when_repair_is_skipped(self):
        shutil.rmtree(modinstall.STASH)
        self.hand_off()
        relaunch.main([])
        self.assertEqual(self.started, [self.exe])
        self.assertIn("repair skipped", self.log_text())
        self.assertFalse(os.path.exists(relaunch.HANDOFF))

    def test_starts_the_game_when_the_game_will_not_close(self):
        self.hand_off()
        self.run_for(self.exe, 15)
        with mock.patch.object(relaunch, "UPDATER_WAIT", 1):
            relaunch.main([])
        self.assertEqual(self.started, [self.exe])
        self.assertIn("repair skipped", self.log_text())
        self.assertFalse(self.registered())

    def test_starts_the_game_when_repair_blows_up(self):
        self.hand_off()
        with mock.patch.object(modinstall, "repair", side_effect=RuntimeError("boom")):
            relaunch.main([])
        self.assertEqual(self.started, [self.exe])
        self.assertIn("repair failed (boom)", self.log_text())

    def test_starts_the_game_when_everything_blows_up(self):
        self.hand_off()
        with mock.patch.object(relaunch, "repair", side_effect=KeyboardInterrupt):
            relaunch.main([])
        self.assertEqual(self.started, [self.exe])

    def test_does_not_wait_forever_on_a_hung_repair(self):
        self.hand_off()
        release = threading.Event()
        self.addCleanup(release.set)
        with mock.patch.object(modinstall, "repair", side_effect=lambda **kw: release.wait(30)), \
                mock.patch.object(relaunch, "REPAIR_LIMIT", 1):
            started = time.monotonic()
            relaunch.main([])
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(self.started, [self.exe])
        self.assertIn("starting the game anyway", self.log_text())

    def test_nothing_to_start(self):
        os.remove(modinstall.RECEIPT)
        self.assertEqual(relaunch.main([]), 0)
        self.assertEqual(self.started, [])
        self.assertIn("start it yourself", self.log_text())

    def test_check(self):
        self.assertEqual(relaunch.main(["--check"]), 0)


@unittest.skipUnless(os.name == "nt", "the Windows relauncher")
class Start(unittest.TestCase):
    """relaunch.start(): from the game's own folder, which is where its next update goes."""

    exe = r"C:\Games\Pokemon TCG Live\Pokemon TCG Live.exe"

    def test_shellexecute_in_the_game_folder(self):
        with mock.patch.object(os, "startfile", create=True) as startfile:
            self.assertEqual(relaunch.start(self.exe), "ShellExecute")
        startfile.assert_called_once_with(self.exe, cwd=r"C:\Games\Pokemon TCG Live")

    def test_old_python_falls_back_to_createprocess_in_the_game_folder(self):
        with mock.patch.object(os, "startfile", create=True, side_effect=TypeError), \
                mock.patch.object(subprocess, "Popen") as popen:
            self.assertEqual(relaunch.start(self.exe), "CreateProcess")
        self.assertEqual(popen.call_args.args[0], [self.exe])
        self.assertEqual(popen.call_args.kwargs["cwd"], r"C:\Games\Pokemon TCG Live")

    def test_shellexecute_failing_falls_back(self):
        with mock.patch.object(os, "startfile", create=True, side_effect=OSError("no")), \
                mock.patch.object(subprocess, "Popen") as popen, \
                mock.patch.object(relaunch, "log"):
            self.assertEqual(relaunch.start(self.exe), "CreateProcess")
        popen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
