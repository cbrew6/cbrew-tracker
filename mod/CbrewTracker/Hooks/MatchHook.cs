using System;
using HarmonyLib;
using CbrewTracker.Emit;
using MatchLogic;
using SharedSDKUtils;
using _Rainier.Scripts.BattleLog;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;

namespace CbrewTracker.Hooks
{
    /// <summary>
    /// Captures one record per match from the client's own match state.
    ///
    /// Three patches:
    ///   NetworkMatchController.OnMatchCreation()                  -> match_start
    ///   NetworkMatchController.StartVersusScene(PlayerDetails[])  -> match_start (direct)
    ///   EndGameHandler.LoadEndBattleScreen(...)                   -> match_end
    ///
    /// All three are read-only postfixes: they observe values the client has already
    /// computed and never influence what the game does with them.
    /// </summary>
    /// <remarks>
    /// The bare class-level [HarmonyPatch] is load-bearing. Harmony's class processor
    /// treats a type with no class-level annotation as "not a patch container" and returns
    /// without patching anything - silently, with no exception - however many method-level
    /// [HarmonyPatch] attributes it carries.
    /// </remarks>
    [HarmonyPatch]
    internal static class MatchHook
    {
        private static EventLog _events;

        // Carried from match start so the end record can be tied to the same match even
        // if currentMatch has already been torn down by the time results are shown.
        private static string _openMatchId;

        // Two hooks cover match start (see below) and both can fire for the same match,
        // so the first one to record wins and the other is ignored.
        private static string _lastRecordedMatchId;

        /// <summary>
        /// The most recent match this launch recorded, or null before the first one.
        /// Read by SeasonRankHook so a standing snapshot can be attached to the match it
        /// followed - the season-rank refresh carries no match id of its own.
        /// </summary>
        public static string LastMatchId => _lastRecordedMatchId;

        private static string _dataDir;

        public static void Initialize(EventLog events, string dataDir)
        {
            _events = events;
            _dataDir = dataDir;
        }

        /// <summary>Player 1 is index 0. Mirrors EndGameHandler.GetWinningPlayerDetails.</summary>
        private static PlayerDetails Local(PlayerDetails[] players, bool isPlayer1) =>
            players[isPlayer1 ? 0 : 1];

        private static PlayerDetails Opponent(PlayerDetails[] players, bool isPlayer1) =>
            players[isPlayer1 ? 1 : 0];

        /// <summary>The client strips a '{...}' suffix before displaying a name; match that.</summary>
        private static string DisplayName(string raw) =>
            string.IsNullOrEmpty(raw) ? "" : raw.Split('{')[0];

        /// <summary>
        /// competitiveElo arrives as 0 outside Arceus League - the game's own PlayerDetails
        /// constructor only copies it when seasonLeagueNumber == 5, server-side, before we
        /// ever see the object. Record that as absent rather than as a rating of zero.
        /// </summary>
        private static object EloOrNull(uint elo) => elo == 0 ? null : (object)elo;

        private static string DeckName(PlayerDetails p)
        {
            try { return p?.deckInfo?.deckName; }
            catch { return null; }
        }

        /// <summary>
        /// The key strings the server uses inside DeckInfo.ServerAuthoritativeMetadata.
        /// Taken from the game's own SERVER_METADATA_*_KEY constants where they are
        /// reachable, so a rename on their side turns into a miss we can see rather than
        /// a column that silently fills with nulls. The literals are only a fallback.
        /// </summary>
        private static readonly string WinsKey = MetadataKey("SERVER_METADATA_WINS_KEY", "wins");
        private static readonly string LossesKey = MetadataKey("SERVER_METADATA_LOSSES_KEY", "losses");

        private static string MetadataKey(string constName, string fallback)
        {
            try
            {
                var f = typeof(DeckInfo).GetField(
                    constName, BindingFlags.Public | BindingFlags.Static | BindingFlags.FlattenHierarchy);
                return (f?.GetValue(null) as string) ?? fallback;
            }
            catch { return fallback; }
        }

        /// <summary>
        /// Where a deck came from and how much it has been worked on.
        ///
        /// Both are private [JsonProperty] fields on DeckInfo, so they are populated but not
        /// reachable without reflection. `creationSource` is a string - observed values are
        /// Copy, Import, ImportRarest, ImportUnowned - and `iterationCount` counts edits.
        /// Together they separate a raw netdeck (Import, 0 iterations) from somebody's own
        /// tuned list, which is the one thing a deck name never tells you.
        ///
        /// Absent rather than defaulted when unreadable: "not reported" and "built from
        /// scratch" are different facts, the same rule DeckRecord follows.
        ///
        /// Read for both players. For the opponent these two fields are all the client
        /// receives about their deck: no cards, no name and no record.
        /// </summary>
        private static void DeckOrigin(PlayerDetails p, out object source, out object iterations)
        {
            source = null;
            iterations = null;
            try
            {
                var info = p?.deckInfo;
                if (info == null) return;

                var type = info.GetType();
                var srcField = type.GetField("creationSource",
                    BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
                if (srcField != null)
                {
                    var raw = srcField.GetValue(info) as string;
                    if (!string.IsNullOrEmpty(raw)) source = raw;
                }

                var iterField = type.GetField("iterationCount",
                    BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
                if (iterField != null) iterations = iterField.GetValue(info);
            }
            catch (Exception ex)
            {
                Log.Info("deck origin unavailable: " + ex.Message);
            }
        }

        /// <summary>
        /// The deck's lifetime record as the *server* reports it, not something we count.
        ///
        /// It rides along on the same object as the decklist and costs no extra call.
        /// Absent is recorded as absent - a deck with no metadata is not a deck with zero
        /// wins. Only the local player's deck carries it.
        ///
        /// Read through IDictionary rather than a concrete generic type, and parsed from
        /// the string form, because the value arrives boxed and its numeric type is not
        /// worth depending on.
        /// </summary>
        private static void DeckRecord(PlayerDetails p, out object wins, out object losses)
        {
            wins = null;
            losses = null;
            try
            {
                var meta = p?.deckInfo?.ServerAuthoritativeMetadata as IDictionary;
                if (meta == null || meta.Count == 0) return;
                wins = MetadataInt(meta, WinsKey);
                losses = MetadataInt(meta, LossesKey);
            }
            catch
            {
                // Never let a metadata shape change cost us the match record itself.
            }
        }

        private static object MetadataInt(IDictionary meta, string key)
        {
            try
            {
                if (key == null || !meta.Contains(key)) return null;
                var raw = meta[key];
                if (raw == null) return null;
                return int.TryParse(
                    Convert.ToString(raw, CultureInfo.InvariantCulture),
                    NumberStyles.Integer, CultureInfo.InvariantCulture, out var n)
                    ? (object)n : null;
            }
            catch { return null; }
        }

        /// <summary>
        /// The local player's decklist, as JSON tuples (format below). The opponent's list
        /// is not sent to the client, so there is no equivalent for them.
        /// </summary>
        private static string DeckList(PlayerDetails p)
        {
            try
            {
                var cards = p?.deckInfo?.cards;
                if (cards == null || cards.Count == 0) return null;

                var keys = new List<string>(cards.Keys);
                keys.Sort(StringComparer.Ordinal);

                // JSON tuples [id, name, count, category]. Card names can contain commas and
                // apostrophes, so a delimited string would need escaping anyway; name and
                // category are null when the card database cannot resolve them, and the id
                // always survives as the canonical key. Category ("Pokemon", "Item", ...) is
                // the deck section, read from the game database so a list sorts offline.
                var sb = new System.Text.StringBuilder("[");
                foreach (var key in keys)
                {
                    if (sb.Length > 1) sb.Append(',');
                    sb.Append('[');
                    Emit.Json.WriteQuoted(sb, key);
                    sb.Append(',');
                    var name = Discovery.CardNames.Get(key);
                    if (name == null) sb.Append("null");
                    else Emit.Json.WriteQuoted(sb, name);
                    sb.Append(',').Append(cards[key]).Append(',');
                    var category = Discovery.CardNames.GetCategory(key);
                    if (category == null) sb.Append("null");
                    else Emit.Json.WriteQuoted(sb, category);
                    sb.Append(']');
                }
                return sb.Append(']').ToString();
            }
            catch
            {
                return null;
            }
        }

        /// <summary>
        /// Every match arrives here: it is the single handler wired to
        /// MatchService.receiveMatchCreated, and it sets currentMatch and isPlayer1
        /// synchronously before we run.
        ///
        /// This is the hook that matters for ladder play. StartVersusScene (below) is only
        /// reached on the direct-match paths - ranked matchmaking goes through
        /// JoinMatchMaking, which invokes a caller-supplied delegate instead.
        /// </summary>
        [HarmonyPatch(typeof(NetworkMatchController), "OnMatchCreation")]
        [HarmonyPostfix]
        public static void OnMatchCreated()
        {
            try
            {
                var match = NetworkMatchController.currentMatch;
                Log.Info($"hook OnMatchCreation fired (currentMatch={(match == null ? "null" : match.matchID)}, "
                       + $"players={match?.players?.Length ?? -1})");
                Record(match?.players, match?.matchID, "OnMatchCreation");
            }
            catch (Exception ex)
            {
                // Never let a recording failure propagate into game code.
                Log.Error($"OnMatchCreation hook failed: {ex}");
            }
        }

        /// <summary>
        /// Direct matches (friend challenges, lobbies) only. Kept because it carries the
        /// player array directly and costs nothing when OnMatchCreation already recorded.
        /// </summary>
        [HarmonyPatch(typeof(NetworkMatchController), nameof(NetworkMatchController.StartVersusScene))]
        [HarmonyPostfix]
        public static void OnVersusScene(PlayerDetails[] details)
        {
            try
            {
                Log.Info($"hook StartVersusScene fired (players={details?.Length ?? -1})");
                Record(details, NetworkMatchController.currentMatchID, "StartVersusScene");
            }
            catch (Exception ex)
            {
                Log.Error($"StartVersusScene hook failed: {ex}");
            }
        }

        private static void Record(PlayerDetails[] details, string matchId, string via)
        {
            if (details == null || details.Length < 2)
            {
                Log.Warn($"match_start skipped ({via}): expected 2 players, got {details?.Length ?? 0}");
                return;
            }

            if (matchId != null && matchId == _lastRecordedMatchId)
                return;   // the other hook already recorded this match

            _lastRecordedMatchId = matchId;
            _openMatchId = matchId;

            var isP1 = NetworkMatchController.isPlayer1;
            var me = Local(details, isP1);
            var opp = Opponent(details, isP1);

            DeckRecord(me, out var myWins, out var myLosses);
            DeckOrigin(me, out var mySource, out var myIters);
            DeckOrigin(opp, out var oppSource, out var oppIters);

            _events.Write(new List<KeyValuePair<string, object>>
            {
                New("schema_version",         EventLog.SchemaVersion),
                New("event_type",             "match_start"),
                New("timestamp",              EventLog.Now()),
                New("match_id",               matchId),
                New("game_mode",              NetworkMatchController.gameMode.ToString()),
                New("gameplay_type",          NetworkMatchController.gameplayType.ToString()),
                New("i_am_player1",           isP1),
                // Which account is playing. Without it a household or a second account
                // folds two ladders into one set of statistics, and there is no way to
                // tell afterwards which match belonged to whom. The same two fields are
                // recorded for the opponent, read off the same object.
                New("my_display_name",        DisplayName(me.playerName)),
                New("my_id_hash",             _events.HashPlayerId(me.playerId)),
                New("my_elo",                 EloOrNull(me.competitiveElo)),
                New("my_exp",                 me.playerExp),
                New("my_deck",                DeckName(me)),
                New("my_decklist",            DeckList(me)),
                New("my_deck_wins",           myWins),
                New("my_deck_losses",         myLosses),
                New("my_deck_source",         mySource),
                New("my_deck_iterations",     myIters),
                New("opponent_display_name",  DisplayName(opp.playerName)),
                New("opponent_id_hash",       _events.HashPlayerId(opp.playerId)),
                New("opponent_elo",           EloOrNull(opp.competitiveElo)),
                // playerExp is the number the versus screen shows outside the top league.
                // It is ranked LADDER POINTS, not account XP: +10 a win plus a +3 streak
                // bonus, and 0 in casual. Elo exists only in the top league (see EloOrNull).
                New("opponent_exp",           opp.playerExp),
                // Nothing else about the opponent's deck reaches the client.
                New("opponent_deck_source",   oppSource),
                New("opponent_deck_iterations", oppIters),
                New("client_version",         Bootstrap.ClientVersion),
                New("capture_method",         "mod"),
            });

            Log.Info(
                $"match_start {matchId} via {via} as {DisplayName(me.playerName)} "
                + $"vs {DisplayName(opp.playerName)} " +
                $"elo={(opp.competitiveElo == 0 ? "none (not Arceus)" : opp.competitiveElo.ToString())} " +
                $"exp={opp.playerExp} " +
                $"({NetworkMatchController.gameplayType}/{NetworkMatchController.gameMode})");
        }

        /// <summary>
        /// The single point every match end passes through.
        ///
        /// LoadEndBattleScreen is the fork: it runs either the full results screen or the
        /// abbreviated one, and only the full branch reaches SetupMatchResultsScreen, so a
        /// hook there would miss every abbreviated end. That is not an edge case. Conceding
        /// takes the abbreviated branch whenever the server does not answer within five
        /// seconds:
        ///
        ///     MatchManager.LoadEndBattleScreen(useAbbreviatedResults: true,
        ///                                      "match_results_defeat_reason_concede")
        ///
        /// passing no EndGameModification at all. Offline matches always take it too.
        ///
        /// Hooking the fork rather than one of its branches costs nothing: modification,
        /// gameEndReasonLocID and __instance are the same objects the results screen would
        /// have been handed.
        /// </summary>
        /// <remarks>
        /// This is an iterator method, so the postfix runs when the coroutine object is
        /// created, not when its body executes - before the results scene loads. That is
        /// the earliest correct moment rather than a compromise: the match is already
        /// decided, and the stopwatch has not yet absorbed the scene-load time that the
        /// game itself excludes when it reads ElapsedMilliseconds at the top of this
        /// method.
        /// </remarks>
        [HarmonyPatch(typeof(EndGameHandler), "LoadEndBattleScreen")]
        [HarmonyPostfix]
        public static void OnEndBattleScreen(
            EndGameHandler __instance,
            bool useAbbreviatedResults,
            string gameEndReasonLocID,
            EndGameModification modification)
        {
            try
            {
                // Logged before the open-match check, so that a gap in the records is
                // distinguishable from the hook never having run at all.
                Log.Info($"hook LoadEndBattleScreen fired (abbreviated={useAbbreviatedResults}, "
                       + $"reason={gameEndReasonLocID}, "
                       + $"stats={(modification == null ? "none" : "present")})");

                if (_openMatchId == null) return;
                var matchId = _openMatchId;
                _openMatchId = null;

                var isP1 = NetworkMatchController.isPlayer1;

                // The same test the game makes in both branches of this method. A null
                // modification means the concede-timeout path, which is always a loss.
                var won = modification != null && modification.winner == (isP1 ? 1 : 2);

                // player1Stats belongs to player 1 whoever won, so the seat is the only
                // thing that selects it. Keying these off the result as well would swap both
                // players' stats on every match that was lost.
                var mine   = isP1 ? modification?.player1Stats : modification?.player2Stats;
                var theirs = isP1 ? modification?.player2Stats : modification?.player1Stats;

                string logPath = null;
                try
                {
                    var data = AccessTools.Field(typeof(EndGameHandler), "_battleLogExportData")
                        ?.GetValue(__instance) as IEnumerable<IBattleLogData>;
                    logPath = BattleLogWriter.Save(
                        System.IO.Path.Combine(_dataDir, "battlelogs"), matchId, data);
                }
                catch (Exception ex)
                {
                    Log.Warn($"battle log not saved: {ex.Message}");
                }

                long? durationMs = null;
                try
                {
                    var sw = AccessTools.Field(typeof(EndGameHandler), "_matchTimeStopWatch")
                        ?.GetValue(__instance) as System.Diagnostics.Stopwatch;
                    if (sw != null) durationMs = sw.ElapsedMilliseconds;
                }
                catch
                {
                    // Duration is a nicety; never fail the record over it.
                }

                _events.Write(new List<KeyValuePair<string, object>>
                {
                    New("schema_version",      EventLog.SchemaVersion),
                    New("event_type",          "match_end"),
                    New("timestamp",           EventLog.Now()),
                    New("match_id",            matchId),
                    New("result",              won ? "win" : "loss"),
                    New("end_reason",          gameEndReasonLocID),
                    // True when the client skipped the full results screen. Those ends
                    // carry no per-player stats, so it also explains their absence.
                    New("abbreviated",         useAbbreviatedResults),
                    New("duration_ms",         durationMs),
                    New("my_damage_dealt",     mine?.damageDealt),
                    New("my_coin_flips_won",   mine?.coinFlipsWon),
                    New("my_mvp_card",         mine?.mvpCardSourceID),
                    New("opponent_damage_dealt",   theirs?.damageDealt),
                    New("opponent_coin_flips_won", theirs?.coinFlipsWon),
                    New("opponent_mvp_card",       theirs?.mvpCardSourceID),
                    New("battle_log",          logPath),
                    New("client_version",      Bootstrap.ClientVersion),
                    New("capture_method",      "mod"),
                });

                Log.Info($"match_end {matchId} result={(won ? "win" : "loss")} "
                       + $"reason={gameEndReasonLocID} "
                       + $"duration={(durationMs.HasValue ? durationMs / 1000 + "s" : "?")} "
                       + $"battlelog={(logPath == null ? "none" : System.IO.Path.GetFileName(logPath))}"
                       + $"{(useAbbreviatedResults ? " (abbreviated - no stats)" : "")}");
            }
            catch (Exception ex)
            {
                Log.Error($"end-battle hook failed: {ex}");
            }
        }

        // There is deliberately no hook on SetupMatchResultsScreen or on
        // GetWinningPlayerDetails.
        //
        // Both sit *inside* the full-results branch of LoadEndBattleScreen, so both miss
        // every abbreviated end - which is how conceding usually finishes. Worse, either
        // one runs while the match is still open and would claim it, starving the hook
        // above of the record. A hook nested inside the thing it is meant to back up is
        // not a fallback.

        private static KeyValuePair<string, object> New(string k, object v) =>
            new KeyValuePair<string, object>(k, v);
    }
}
