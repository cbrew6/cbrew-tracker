using System;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using System.Text;
using HarmonyLib;
using CbrewTracker.Emit;

namespace CbrewTracker.Hooks
{
    /// <summary>
    /// Records the player's own ranked standing whenever the client refreshes it.
    ///
    ///     SeasonRankQuery.UpdateInfoCache()       -> season_rank
    ///
    /// This is what makes Elo *live*. PlayerDetails only ever carries a rating at match
    /// start, so without this a match's own result would be visible only as the difference
    /// to the next match's opening rating - the newest game would have no delta until
    /// another was played. The season-rank cache holds what the server sends back *after* a
    /// match, so the change lands as soon as the client asks for it.
    ///
    /// It also carries two things that would otherwise have to be derived or guessed:
    ///
    ///   previousMatchExpDelta  the ladder points that last match was worth, stated by the
    ///                          server - so no points formula has to be reimplemented
    ///   consecutiveWins        the win streak the game itself is counting, rather than one
    ///                          reconstructed from our own ledger
    ///
    /// Read-only postfix. The client fetched this for its own account and we observe where
    /// it lands; nothing here asks the server for anything.
    /// </summary>
    /// <remarks>
    /// Reached entirely by reflection, for the same reasons as Discovery/CardNames: the
    /// types sit in an SDK namespace whose accessibility we do not control, the chain
    /// crosses assemblies, and the tracker is expensive to iterate on. Reflection degrades
    /// to "no snapshot, one log line" rather than refusing to compile or throwing mid-game.
    ///
    /// The bare class-level [HarmonyPatch] is load-bearing - see MatchHook.
    /// </remarks>
    [HarmonyPatch]
    internal static class SeasonRankHook
    {
        private static EventLog _events;

        // UpdateInfoCache is a cache refresh and is called far more often than the values
        // change. Writing an event per call would bury the log in duplicates, so the last
        // written snapshot is kept and an identical one is dropped.
        private static string _last;

        // The one type that actually holds the cache. A missing type is skipped rather than
        // failing the whole patch set.
        //
        // LocalSeasonRankQuery is deliberately not patched: it is the offline stub. Every
        // property is a hardcoded constant (exp => 0u, competitiveEloDefault => 1500u) and its
        // UpdateInfoCache is a bare `throw new NotImplementedException()`, so a postfix there
        // can never run - Harmony skips postfixes when the original throws.
        private static readonly string[] QueryTypes =
        {
            "RainierClientSDK.source.SeasonRank.SeasonRankQuery",
        };

        // Property name -> the field name it is written out as. Every one is optional:
        // anything the client stops exposing is recorded as absent rather than as zero,
        // which is the rule competitiveElo already follows outside the top league.
        private static readonly string[][] Fields =
        {
            new[] { "competitiveElo",        "elo" },
            new[] { "competitiveEloDefault", "elo_default" },
            new[] { "exp",                   "exp" },
            new[] { "highestExp",            "highest_exp" },
            new[] { "wins",                  "wins" },
            new[] { "losses",                "losses" },
            new[] { "seasonMatches",         "season_matches" },
            new[] { "consecutiveWins",       "consecutive_wins" },
            new[] { "previousMatchExpDelta", "previous_match_exp_delta" },
            new[] { "currentSeason",         "season" },
        };

        public static void Initialize(EventLog events)
        {
            _events = events;
        }

        /// <summary>
        /// Every UpdateInfoCache we can find. Returning an empty list is not fatal here:
        /// Bootstrap's "patched nothing at all" check is what catches a total failure, and
        /// this hook going missing must never take match capture down with it.
        /// </summary>
        private static IEnumerable<MethodBase> TargetMethods()
        {
            var found = new List<MethodBase>();
            foreach (var name in QueryTypes)
            {
                try
                {
                    var type = AccessTools.TypeByName(name);
                    if (type == null)
                    {
                        Log.Info("season rank: no type " + name + " (skipped)");
                        continue;
                    }

                    var method = AccessTools.Method(type, "UpdateInfoCache");
                    if (method == null)
                    {
                        Log.Info("season rank: " + name + " has no UpdateInfoCache (skipped)");
                        continue;
                    }

                    found.Add(method);
                }
                catch (Exception ex)
                {
                    Log.Error("season rank: resolving " + name + " failed: " + ex.Message);
                }
            }

            if (found.Count == 0)
            {
                Log.Error("season rank: nothing to patch - live Elo will not be recorded. "
                        + "The season-rank cache type has probably moved.");
            }
            return found;
        }

        /// <summary>
        /// __instance is typed as object deliberately: the declaring type may not be
        /// public, and naming it here would be a compile-time dependency on an SDK
        /// internal that a game update is free to rename.
        /// </summary>
        [HarmonyPostfix]
        private static void Postfix(object __instance)
        {
            try
            {
                if (_events == null || __instance == null) return;

                var type = __instance.GetType();
                var values = new List<KeyValuePair<string, object>>();
                var seen = 0;

                foreach (var pair in Fields)
                {
                    object value = null;
                    try
                    {
                        var prop = AccessTools.Property(type, pair[0]);
                        if (prop != null && prop.CanRead) value = prop.GetValue(__instance, null);
                    }
                    catch
                    {
                        // One unreadable property must not cost the whole snapshot.
                    }

                    value = Scalar(pair[1], value);
                    if (value != null) seen++;
                    values.Add(new KeyValuePair<string, object>(pair[1], value));
                }

                // Nothing readable at all means the shape moved. Say so once, rather than
                // writing a row of nulls on every refresh for the rest of the session.
                if (seen == 0)
                {
                    if (_last != "empty")
                    {
                        _last = "empty";
                        Log.Error("season rank: " + type.FullName + " exposed none of the "
                                + "expected properties; snapshot not recorded.");
                    }
                    return;
                }

                var key = Fingerprint(values);
                if (key == _last) return;
                _last = key;

                var record = new List<KeyValuePair<string, object>>
                {
                    New("schema_version", EventLog.SchemaVersion),
                    New("event_type",     "season_rank"),
                    New("timestamp",      EventLog.Now()),
                    // The match this standing follows, so the projection can attach a
                    // post-match rating to the game that produced it. Null before any match
                    // has been recorded this launch, which is normal at startup.
                    New("match_id",       MatchHook.LastMatchId),
                };
                record.AddRange(values);
                record.Add(New("client_version", Bootstrap.ClientVersion));
                record.Add(New("capture_method", "mod"));

                _events.Write(record);

                Log.Info("season_rank " + Describe(values));
            }
            catch (Exception ex)
            {
                Log.Error("season-rank hook failed: " + ex);
            }
        }

        /// <summary>
        /// Flattens a per-game-mode value to the one number this tracker records.
        ///
        /// competitiveElo is NOT an int - it is Dictionary&lt;MatchLogic.GameMode, uint&gt;,
        /// because the client keeps a separate rating per mode. Written as-is, the
        /// dictionary's ToString() would flow through ingest into an INTEGER column (SQLite
        /// will happily store a string there) and break any arithmetic on it. Hence: flatten
        /// here, at the source.
        ///
        /// Standard is the ranked ladder this tracker is about. A map with no Standard entry
        /// and more than one mode returns null - absent rather than a guess, the same rule
        /// competitiveElo already follows outside the top league.
        /// </summary>
        private static object Scalar(string field, object value)
        {
            var dict = value as System.Collections.IDictionary;
            if (dict == null) return value;

            object only = null;
            var count = 0;
            var modes = new StringBuilder();
            foreach (System.Collections.DictionaryEntry entry in dict)
            {
                count++;
                only = entry.Value;
                if (modes.Length > 0) modes.Append(' ');
                modes.Append(Convert.ToString(entry.Key, CultureInfo.InvariantCulture))
                     .Append('=')
                     .Append(Convert.ToString(entry.Value, CultureInfo.InvariantCulture));

                if (string.Equals(Convert.ToString(entry.Key, CultureInfo.InvariantCulture),
                                  "Standard", StringComparison.OrdinalIgnoreCase))
                {
                    return entry.Value;
                }
            }

            if (count == 1) return only;

            // Worth one line: an empty map is the normal state below the top league, but a
            // populated one we could not read is a shape change and should be visible.
            if (count > 1 && _reportedModes != modes.ToString())
            {
                _reportedModes = modes.ToString();
                Log.Info("season rank: " + field + " has no Standard entry; modes were "
                       + modes + " (recorded as absent)");
            }
            return null;
        }

        private static string _reportedModes;

        private static string Fingerprint(List<KeyValuePair<string, object>> values)
        {
            var sb = new StringBuilder();
            foreach (var v in values)
            {
                sb.Append(v.Key).Append('=');
                sb.Append(v.Value == null
                    ? "?"
                    : Convert.ToString(v.Value, CultureInfo.InvariantCulture));
                sb.Append(';');
            }
            return sb.ToString();
        }

        private static string Describe(List<KeyValuePair<string, object>> values)
        {
            var sb = new StringBuilder();
            foreach (var v in values)
            {
                if (v.Value == null) continue;
                if (sb.Length > 0) sb.Append(' ');
                sb.Append(v.Key).Append('=')
                  .Append(Convert.ToString(v.Value, CultureInfo.InvariantCulture));
            }
            return sb.Length == 0 ? "(nothing readable)" : sb.ToString();
        }

        private static KeyValuePair<string, object> New(string k, object v) =>
            new KeyValuePair<string, object>(k, v);
    }
}
