using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;

namespace CbrewTracker.Emit
{
    /// <summary>
    /// Append-only JSONL sink. One JSON object per line, flushed per write, opened and
    /// closed per event so an unclean game exit cannot lose more than the event in flight.
    /// Writes are cheap and infrequent (twice per match), so this costs nothing in practice.
    /// </summary>
    internal sealed class EventLog
    {
        public const int SchemaVersion = 1;

        private readonly string _path;
        private readonly byte[] _salt;
        private readonly object _lock = new object();

        public string Path => _path;

        public EventLog(string directory)
        {
            Directory.CreateDirectory(directory);
            _path = System.IO.Path.Combine(directory, "events.jsonl");
            _salt = LoadOrCreateSalt(System.IO.Path.Combine(directory, "salt"));
        }

        /// <summary>
        /// Per-install random salt for opponent id hashing. Kept out of the event log so
        /// the log can be shared without making the hashes reversible by dictionary attack.
        /// </summary>
        private static byte[] LoadOrCreateSalt(string saltPath)
        {
            if (File.Exists(saltPath))
            {
                var existing = File.ReadAllBytes(saltPath);
                if (existing.Length >= 16) return existing;
            }

            var salt = new byte[32];
            using (var rng = RandomNumberGenerator.Create()) rng.GetBytes(salt);
            File.WriteAllBytes(saltPath, salt);
            return salt;
        }

        /// <summary>
        /// Stable pseudonym for a player id. Lets a future shared pool recognise a repeat
        /// opponent without carrying their account id or display name.
        /// </summary>
        public string HashPlayerId(string playerId)
        {
            if (string.IsNullOrEmpty(playerId)) return null;

            using (var sha = SHA256.Create())
            {
                var idBytes = Encoding.UTF8.GetBytes(playerId);
                var buf = new byte[_salt.Length + idBytes.Length];
                Buffer.BlockCopy(_salt, 0, buf, 0, _salt.Length);
                Buffer.BlockCopy(idBytes, 0, buf, _salt.Length, idBytes.Length);

                var hash = sha.ComputeHash(buf);
                var sb = new StringBuilder(32);
                for (var i = 0; i < 16; i++) sb.Append(hash[i].ToString("x2", CultureInfo.InvariantCulture));
                return sb.ToString();
            }
        }

        public void Write(List<KeyValuePair<string, object>> fields)
        {
            var line = Json.Object(fields);
            lock (_lock)
            {
                using (var fs = new FileStream(_path, FileMode.Append, FileAccess.Write, FileShare.Read))
                using (var w = new StreamWriter(fs, new UTF8Encoding(false)))
                {
                    w.WriteLine(line);
                    w.Flush();
                }
            }
        }

        public static string Now() =>
            DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffZ", CultureInfo.InvariantCulture);
    }
}
