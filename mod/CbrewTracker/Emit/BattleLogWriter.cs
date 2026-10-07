using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using _Rainier.Scripts.BattleLog;

namespace CbrewTracker.Emit
{
    /// <summary>
    /// Saves the in-game battle log to disk at the end of every match.
    ///
    /// The game already builds this text for its Export button
    /// (BattleLogExporter.ExportBattleLog), but only on a click, and it writes the result
    /// to the system clipboard. This reproduces the same formatting from the same data so
    /// the log can be saved automatically without clobbering whatever the player has
    /// copied - a tracker should not take the clipboard hostage.
    ///
    /// Contains only what the battle log already showed on screen during the match.
    /// </summary>
    internal static class BattleLogWriter
    {
        /// <summary>Writes the log and returns the file path, or null if there was nothing to write.</summary>
        public static string Save(string directory, string matchId, IEnumerable<IBattleLogData> data)
        {
            if (data == null) return null;

            var text = Format(data);
            if (string.IsNullOrEmpty(text)) return null;

            Directory.CreateDirectory(directory);
            var path = Path.Combine(directory, SafeName(matchId) + ".txt");
            File.WriteAllText(path, text, new UTF8Encoding(false));
            return path;
        }

        /// <summary>Mirrors BattleLogExporter.ExportBattleLog's output format.</summary>
        private static string Format(IEnumerable<IBattleLogData> data)
        {
            var sb = new StringBuilder();

            foreach (var phase in data.OfType<IBattleLogPhaseData>())
            {
                sb.AppendLine(
                    phase.BattlePhase == PhaseType.Player || phase.BattlePhase == PhaseType.Opponent
                        ? phase.PlainTextPhaseTitle
                        : phase.PhaseTitle ?? "");

                foreach (var entry in phase.LogEntries.OfType<IBattleLogMainEntryData>())
                {
                    sb.AppendLine(entry.PlainTextDisplayString ?? "");

                    foreach (var sub in entry.SubEntries.OfType<IBattleLogSubEntryData>())
                    {
                        sb.AppendLine("- " + sub.PlainTextDisplayString);
                        if (!string.IsNullOrEmpty(sub.PlainTextSubString))
                            sb.AppendLine("   - " + sub.PlainTextSubString);
                    }
                }

                sb.AppendLine("");
            }

            return sb.ToString();
        }

        /// <summary>Match ids contain ':', which is not safe in a filename on every platform.</summary>
        private static string SafeName(string matchId)
        {
            if (string.IsNullOrEmpty(matchId)) return "unknown-" + DateTime.UtcNow.Ticks;

            var sb = new StringBuilder(matchId.Length);
            foreach (var c in matchId)
                sb.Append(char.IsLetterOrDigit(c) || c == '-' || c == '_' ? c : '_');
            return sb.ToString();
        }
    }
}
