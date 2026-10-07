using System;
using System.Collections.Generic;
using Process = System.Diagnostics.Process;
using System.IO;
using System.Reflection;
using HarmonyLib;
using CbrewTracker.Emit;
using UnityEngine;

namespace CbrewTracker.Hooks
{
    /// <summary>
    /// Makes a game update put the tracker back before the updated game first starts.
    ///
    /// A Pokemon TCG Live update is a hand-off between two programs. On macOS:
    ///
    ///     StartupScreenText.OpenAppUpdater                  (in the game, this mod loaded)
    ///         Process.Start(".../Updater.app/Contents/MacOS/Updater",
    ///                       "... --installPath \"&lt;app&gt;/\" ... --launchAppAt \"&lt;app&gt;/\"")
    ///         Application.Quit()
    ///     UpdaterComponent.OnPlayButtonClicked              (in the updater, once copied)
    ///         Process.Start("open", "\"&lt;launchAppAt&gt;\"")
    ///
    /// and on Windows the same shape with different ends:
    ///
    ///     StartupScreenText.OpenAppUpdater
    ///         Process.Start("&lt;game&gt;\Updater\&lt;version&gt;\Updater.exe",
    ///                       "... --installPath \"&lt;cwd&gt;\" ... --launchAppAt \"&lt;game&gt;\...exe\" ...")
    ///         Application.Quit()
    ///     UpdaterComponent.OnPlayButtonClicked
    ///         Process.Start(launchAppAt, launchAppWithArgs)
    ///
    /// The copy in the middle overwrites both Unity manifests, so the game the updater opens
    /// at the end starts without the tracker - and a session that starts without it records
    /// nothing until the game is restarted. Nothing outside the game can win that race: the
    /// updater opens the game the instant Play is clicked.
    ///
    /// So this changes where the updater hands over, on the way out. On macOS --launchAppAt is
    /// pointed at the tracker's relauncher app (~/.ptcgl-tracker/cbrew Tracker.app). On
    /// Windows the updater runs a program rather than opening a path, so --launchAppAt becomes
    /// the interpreter and --launchAppWithArgs is added to carry the relauncher script
    /// (scripts/relaunch.py) to it. Either way the relauncher re-registers the tracker and
    /// then opens the game itself. Pokemon's updater is not modified in any way; it is simply
    /// handed a different thing to open when it is done.
    ///
    /// **This is the one hook that changes what the client does.** Every other hook is a
    /// read-only postfix. It is scoped as tightly as that allows: only the game's own call to
    /// start its own updater, only on macOS and Windows, only when the relauncher is actually
    /// present to receive the hand-off - a missing relauncher would leave the updated game
    /// unopened, which is far worse than an updated game opened without the tracker. Every
    /// other call, and every failure in here, leaves the arguments exactly as they were.
    /// </summary>
    /// <remarks>
    /// Process.Start(string, string) is a BCL method, patched rather than OpenAppUpdater
    /// itself because the arguments are a local string that method builds and passes straight
    /// on - a prefix on OpenAppUpdater would see nothing to change. It is also the one call
    /// the game makes with that overload (verified on 1.42.0 on macOS, and on 1.42.2 on
    /// Windows, where OpenAppUpdater is the only caller); the updater-path filter would
    /// exclude any other caller regardless.
    ///
    /// The bare class-level [HarmonyPatch] is load-bearing - see MatchHook.
    /// </remarks>
    [HarmonyPatch]
    internal static class UpdaterHandoffHook
    {
        public const string RelauncherName = "cbrew Tracker.app";

        private enum Os { Other, Mac, Windows }

        private static string _dataDir;

        // Decided once, in Prepare(), so that Prefix never touches UnityEngine. That keeps the
        // whole hand-off callable outside the game, which is how it is tested against the
        // real updater - the same compiled method, not a copy of it.
        private static Os _os;

        public static void Initialize(string dataDir)
        {
            _dataDir = dataDir;
        }

        /// <summary>
        /// macOS and Windows only, the two platforms whose updater has been read. Anywhere
        /// else a hand-off redirected to something that cannot receive it would leave the game
        /// closed after an update. Returning false here means Harmony does not patch at all.
        /// </summary>
        private static bool Prepare()
        {
            switch (Application.platform)
            {
                case RuntimePlatform.OSXPlayer:
                    _os = Os.Mac;
                    break;
                case RuntimePlatform.WindowsPlayer:
                    _os = Os.Windows;
                    break;
                default:
                    _os = Os.Other;
                    break;
            }
            return _os != Os.Other;
        }

        private static MethodBase TargetMethod()
        {
            return typeof(Process).GetMethod("Start", BindingFlags.Public | BindingFlags.Static,
                null, new[] { typeof(string), typeof(string) }, null);
        }

        [HarmonyPrefix]
        private static void Prefix(string fileName, ref string arguments)
        {
            try
            {
                if (_os == Os.Windows)
                {
                    PrefixWindows(fileName, ref arguments);
                    return;
                }
                if (_os != Os.Mac) return;

                if (!UpdaterHandoff.IsUpdaterLaunch(fileName)) return;

                Log.Info("game is starting its updater - a game update is about to be installed");

                if (string.IsNullOrEmpty(_dataDir))
                {
                    Log.Warn("updater hand-off: no data directory, leaving the updater alone");
                    return;
                }

                var relauncher = Path.Combine(_dataDir, RelauncherName);
                var executable = Path.Combine(Path.Combine(Path.Combine(relauncher, "Contents"),
                    "MacOS"), "relaunch");
                if (!File.Exists(executable))
                {
                    Log.Warn("updater hand-off: no relauncher at " + relauncher
                        + " - the update will open the game without the tracker, and the "
                        + "watcher will put it back once the game is closed");
                    return;
                }

                string original;
                var redirected = UpdaterHandoff.Redirect(arguments, relauncher, out original);
                if (redirected == null)
                {
                    Log.Warn("updater hand-off: the updater's arguments are not in a recognised "
                        + "shape, leaving them alone: " + arguments);
                    return;
                }

                // Written before the arguments change, so the relauncher always knows exactly
                // what the updater would have opened. It has its own fallbacks if this fails.
                string error;
                if (!WriteHandoff(original, out error))
                    Log.Warn("updater hand-off: could not record the hand-off (" + error
                        + "); the relauncher will find the game from the install receipt");

                arguments = redirected;
                Log.Info("updater hand-off: the updater will open the relauncher instead of "
                    + original + ", which puts the tracker back and then opens the game");
            }
            catch (Exception ex)
            {
                // The original call always goes ahead. The worst this can cost is the
                // seamless re-attach, never the update itself.
                Log.Error("updater hand-off failed, update proceeds unchanged: " + ex.Message);
            }
        }

        /// <summary>
        /// The Windows hand-off. Called inside Prefix's try, so anything thrown here leaves
        /// the arguments as they were.
        /// </summary>
        private static void PrefixWindows(string fileName, ref string arguments)
        {
            if (!UpdaterHandoff.IsWindowsUpdaterLaunch(fileName)) return;

            Log.Info("game is starting its updater - a game update is about to be installed");

            if (string.IsNullOrEmpty(_dataDir))
            {
                Log.Warn("updater hand-off: no data directory, leaving the updater alone");
                return;
            }

            string programArguments, why;
            var program = UpdaterHandoff.ReadWindowsRelauncher(_dataDir, out programArguments,
                out why);
            if (program == null)
            {
                Log.Warn("updater hand-off: " + why + " - the update will open the game "
                    + "without the tracker, and the watcher will put it back once the game "
                    + "is closed");
                return;
            }

            string original;
            var redirected = UpdaterHandoff.RedirectToProgram(arguments, program,
                programArguments, out original);
            if (redirected == null)
            {
                Log.Warn("updater hand-off: the updater's arguments are not in a recognised "
                    + "shape, leaving them alone: " + arguments);
                return;
            }

            // Required here, unlike on macOS. The macOS relauncher can always fall back to
            // opening the game by its bundle id; Windows has no such thing, so the relauncher
            // finding the game rests on this file and the install receipt. Not being able to
            // write it is reason enough to leave the update exactly as Pokemon made it.
            string error;
            if (!WriteHandoff(original, out error))
            {
                Log.Warn("updater hand-off: could not record the hand-off (" + error
                    + "), leaving the updater alone");
                return;
            }

            arguments = redirected;
            Log.Info("updater hand-off: the updater will run the relauncher instead of "
                + original + ", which puts the tracker back and then opens the game");
            // The whole line, once. Quoting is the part of this that can go wrong on Windows,
            // and this is the only place the exact string the updater received is recorded.
            Log.Info("updater hand-off: updater arguments now: " + arguments);
        }

        private static bool WriteHandoff(string original, out string error)
        {
            error = null;
            try
            {
                var fields = new List<KeyValuePair<string, object>>
                {
                    new KeyValuePair<string, object>("launch_app_at", original),
                    new KeyValuePair<string, object>("handed_off_at", EventLog.Now()),
                    new KeyValuePair<string, object>("client_version", Bootstrap.ClientVersion),
                };
                var path = Path.Combine(_dataDir, "handoff.json");
                File.WriteAllText(path + ".tmp", Json.Object(fields));
                if (File.Exists(path)) File.Delete(path);
                File.Move(path + ".tmp", path);
                return true;
            }
            catch (Exception ex)
            {
                error = ex.Message;
                return false;
            }
        }
    }
}
