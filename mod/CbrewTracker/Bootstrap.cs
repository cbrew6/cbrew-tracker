using System;
using System.IO;
using System.Reflection;
using System.Linq;
using System.Runtime.CompilerServices;
using HarmonyLib;
using CbrewTracker.Discovery;
using CbrewTracker.Emit;
using CbrewTracker.Hooks;
using UnityEngine;

namespace CbrewTracker
{
    /// <summary>
    /// Entry point. The tracker is registered in the game's own
    /// RuntimeInitializeOnLoads.json manifest, so Unity calls <see cref="Init"/> during
    /// startup and the assembly loads as if it shipped with the game.
    ///
    /// This replaces the usual BepInEx/Doorstop route, which cannot work here: Unity 6's
    /// UnityPlayer.dylib imports no Mono symbols (it resolves them through dlsym), so
    /// Doorstop's symbol-rebinding has nothing to rebind and silently does nothing.
    ///
    /// Every match hook is a read-only postfix: the tracker observes values the client has
    /// already computed for itself and never calls out to Pokemon from inside the client.
    /// The single exception is <see cref="UpdaterHandoffHook"/>, which changes where the
    /// game's updater hands over when an update finishes - nothing to do with play, and
    /// macOS and Windows only.
    /// </summary>
    public static class Bootstrap
    {
        private const string HarmonyId = "com.cbrew.tracker";

        public static string ClientVersion { get; private set; } = "unknown";

        private static bool _started;

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        public static void Init()
        {
            if (_started) return;   // manifest entries can fire more than once
            _started = true;

            var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            if (string.IsNullOrEmpty(home)) home = Environment.GetEnvironmentVariable("HOME") ?? ".";
            var dataDir = Path.Combine(home, ".ptcgl-tracker");

            Log.Open(dataDir);

            try
            {
                ClientVersion = Application.version;
            }
            catch
            {
                // Not fatal; the field is only used to stamp records.
            }

            Log.Info($"cbrew Tracker starting (client {ClientVersion}).");

            EventLog events;
            try
            {
                events = new EventLog(dataDir);
            }
            catch (Exception ex)
            {
                Log.Error($"Could not open event log in {dataDir}; tracker disabled. {ex}");
                return;
            }

            MatchHook.Initialize(events, dataDir);
            SeasonRankHook.Initialize(events);
            UpdaterHandoffHook.Initialize(dataDir);

            try
            {
                var patched = ApplyHooks();
                Log.Info($"Hooks applied. Recording to {events.Path}");
                if (patched.Length == 0)
                    throw new InvalidOperationException(
                        "Harmony patched nothing. Check that MatchHook still carries a "
                        + "class-level [HarmonyPatch] attribute - without it the class "
                        + "processor silently does nothing.");
                foreach (var m in patched)
                    Log.Info($"    patched {m.DeclaringType?.FullName}.{m.Name}");
            }
            catch (Exception ex)
            {
                Log.Error($"Failed to apply hooks: {ex}");
                DumpSymbolsSafely();
            }
        }

        /// <summary>
        /// Kept out of <see cref="Init"/> and un-inlined on purpose. Mono resolves every
        /// type a method references when it JITs that method, so if Harmony fails to load,
        /// touching it inside Init would abort before the first log line and leave nothing
        /// to debug with. Isolated here, the failure is caught and logged.
        /// </summary>
        [MethodImpl(MethodImplOptions.NoInlining)]
        private static System.Reflection.MethodBase[] ApplyHooks()
        {
            var harmony = new Harmony(HarmonyId);
            harmony.CreateClassProcessor(typeof(MatchHook)).Patch();
            // Separate processor on purpose: the season-rank types are resolved by name at
            // patch time, so a game update that moves them must cost live Elo and nothing
            // else. Match capture is the thing that must never go down with it.
            try
            {
                harmony.CreateClassProcessor(typeof(SeasonRankHook)).Patch();
            }
            catch (Exception ex)
            {
                Log.Error($"Season-rank hook could not be applied (live Elo disabled): {ex.Message}");
            }
            // Separate again, for the same reason. This one patches a BCL method rather than a
            // game method, and if that ever fails the cost must be the seamless re-attach after
            // an update - never match capture. Its Prepare() skips it entirely on any platform
            // whose updater has not been read (everything but macOS and Windows).
            try
            {
                harmony.CreateClassProcessor(typeof(UpdaterHandoffHook)).Patch();
            }
            catch (Exception ex)
            {
                Log.Error($"Updater hand-off hook could not be applied (updates will need a game restart to re-attach): {ex.Message}");
            }
            // No hook on TrackedStats(PlayerEntity, bool), although it looks like the place
            // per-match statistics come from. It lives in SharedLogicUtils, which is shared
            // with the server, and the server is what runs that constructor - a patch on it
            // applies cleanly and never fires. Turns, prizes, knockouts and who went first
            // are read from the saved battle log instead; see ingest.match_shape_from_log().
            return harmony.GetPatchedMethods().ToArray();
        }

        /// <summary>
        /// A game update renaming a hook target is the likely cause of a patch failure, so
        /// name the candidates rather than just reporting that it broke.
        /// </summary>
        [MethodImpl(MethodImplOptions.NoInlining)]
        private static void DumpSymbolsSafely()
        {
            try
            {
                Log.Error("Dumping candidate symbols - the hook targets may have been renamed.");
                SymbolProbe.FindTypes("NetworkMatchController");
                SymbolProbe.FindTypes("EndGameHandler");
            }
            catch (Exception ex)
            {
                Log.Error($"Symbol dump failed too: {ex.Message}");
            }
        }
    }
}
