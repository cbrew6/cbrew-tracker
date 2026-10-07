using System;
using System.IO;
using System.Text;

namespace CbrewTracker.Emit
{
    /// <summary>
    /// Minimal file logger.
    ///
    /// The tracker loads as one of the game's own assemblies rather than through a mod
    /// framework, so there is no host logger to borrow. Everything is wrapped in try/catch:
    /// a logging failure must never surface as a game crash.
    /// </summary>
    internal static class Log
    {
        private static string _path;
        private static readonly object Lock = new object();

        public static void Open(string directory)
        {
            try
            {
                Directory.CreateDirectory(directory);
                _path = Path.Combine(directory, "tracker.log");

                // Truncate per launch - this is a diagnostic log, not an audit trail.
                File.WriteAllText(_path, $"=== cbrew Tracker log, opened {EventLog.Now()} ==={Environment.NewLine}");
            }
            catch
            {
                _path = null;
            }
        }

        public static void Info(string msg) => Write("INFO ", msg);
        public static void Warn(string msg) => Write("WARN ", msg);
        public static void Error(string msg) => Write("ERROR", msg);

        private static void Write(string level, string msg)
        {
            if (_path == null) return;
            try
            {
                lock (Lock)
                {
                    File.AppendAllText(
                        _path,
                        $"[{DateTime.Now:HH:mm:ss}] {level} {msg}{Environment.NewLine}",
                        new UTF8Encoding(false));
                }
            }
            catch
            {
                // A full disk or a permissions problem must not take the game down.
            }
        }
    }
}
