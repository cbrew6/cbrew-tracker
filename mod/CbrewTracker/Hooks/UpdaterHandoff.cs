using System;
using System.IO;
using System.Text;

namespace CbrewTracker.Hooks
{
    /// <summary>
    /// The string handling behind <see cref="UpdaterHandoffHook"/>, kept free of Unity and
    /// Harmony so it can be compiled into a test harness on its own. Everything that decides
    /// whether the game's updater hand-off is touched, and how, is in here.
    /// </summary>
    internal static class UpdaterHandoff
    {
        /// <summary>The tail of the updater's executable path on macOS.</summary>
        public const string MacUpdaterSuffix = "/Updater.app/Contents/MacOS/Updater";

        /// <summary>
        /// What the Windows relauncher is, written by scripts/install-watcher-windows.ps1:
        /// the interpreter on the first line, the script it runs on the second.
        /// </summary>
        public const string WindowsRelauncherFile = "relauncher.txt";

        private const string LaunchFlag = "--launchAppAt \"";
        private const string LaunchArgsFlag = "--launchAppWithArgs";

        /// <summary>
        /// Is this the game starting its own updater (macOS)?
        ///
        /// Matched on the executable's path rather than on anything in the arguments: the
        /// path is fixed by how the game finds its updater (Resources/Updater/&lt;version&gt;/
        /// Updater.app), while the arguments are free to change shape in any update.
        /// </summary>
        public static bool IsUpdaterLaunch(string fileName)
        {
            return !string.IsNullOrEmpty(fileName)
                && fileName.EndsWith(MacUpdaterSuffix, StringComparison.Ordinal);
        }

        /// <summary>
        /// Is this the game starting its own updater (Windows)?
        ///
        /// The game builds the path as Path.GetFullPath(dataPath + "/../Updater/" +
        /// appPatcherVersion + "/Updater.exe"), so it ends &lt;game&gt;\Updater\&lt;version&gt;\Updater.exe.
        /// Either separator is accepted, and case is ignored, because both are the file
        /// system's business on Windows rather than the game's - a matcher that missed on a
        /// forward slash would silently cost the seamless re-attach and nothing else.
        /// </summary>
        public static bool IsWindowsUpdaterLaunch(string fileName)
        {
            if (string.IsNullOrEmpty(fileName)) return false;
            var parts = fileName.Split('\\', '/');
            var n = parts.Length;
            return n >= 3
                && string.Equals(parts[n - 1], "Updater.exe", StringComparison.OrdinalIgnoreCase)
                && parts[n - 2].Length > 0
                && string.Equals(parts[n - 3], "Updater", StringComparison.OrdinalIgnoreCase);
        }

        /// <summary>
        /// The same arguments with --launchAppAt pointing at <paramref name="relauncher"/>, or
        /// null if they are not in a shape this recognises - in which case the caller must
        /// leave them exactly as they were.
        ///
        /// Only the --launchAppAt value is replaced. --installPath carries the very same path
        /// and must not move, or the update would be copied into the relauncher instead of
        /// the game; that is why this anchors on the flag rather than on the path.
        /// </summary>
        public static string Redirect(string arguments, string relauncher, out string original)
        {
            original = null;
            if (string.IsNullOrEmpty(arguments) || string.IsNullOrEmpty(relauncher)) return null;
            if (relauncher.IndexOf('"') >= 0) return null;     // could not be quoted safely

            var flag = arguments.IndexOf(LaunchFlag, StringComparison.Ordinal);
            if (flag < 0) return null;
            if (arguments.IndexOf(LaunchFlag, flag + 1, StringComparison.Ordinal) >= 0)
                return null;                                    // two of them: ambiguous

            var start = flag + LaunchFlag.Length;
            var end = arguments.IndexOf('"', start);
            if (end < 0) return null;                           // unterminated quote

            original = arguments.Substring(start, end - start);
            if (original.Length == 0) return null;
            if (string.Equals(original, relauncher, StringComparison.Ordinal)) return null;

            return arguments.Substring(0, start) + relauncher + arguments.Substring(end);
        }

        /// <summary>
        /// Windows: --launchAppAt pointed at <paramref name="program"/>, and --launchAppWithArgs
        /// added to carry <paramref name="programArguments"/> to it. Null if the arguments are
        /// not in a shape this recognises, exactly as <see cref="Redirect"/>.
        ///
        /// Windows needs the second flag because its updater finishes with
        /// Process.Start(launchAppAt, launchAppWithArgs) - it runs a program, where the macOS
        /// one hands a path to `open`. The relauncher is a Python script, so the program is
        /// the interpreter and the script travels as its argument.
        ///
        /// Refused outright if the game already passes --launchAppWithArgs. Those would be
        /// arguments the game meant for its own next launch; replacing them would drop them,
        /// and carrying them through is a shape nobody has seen yet.
        /// </summary>
        public static string RedirectToProgram(string arguments, string program,
            string programArguments, out string original)
        {
            original = null;
            if (string.IsNullOrEmpty(programArguments)) return null;
            if (arguments != null && arguments.IndexOf(LaunchArgsFlag, StringComparison.Ordinal) >= 0)
                return null;

            string found;
            var redirected = Redirect(arguments, program, out found);
            if (redirected == null) return null;

            // Straight after the --launchAppAt value's closing quote, which Redirect has just
            // proved is there. Appending at the very end instead would trust the rest of the
            // string to be well-formed, and one stray quote anywhere would swallow the flag.
            var at = redirected.IndexOf(LaunchFlag, StringComparison.Ordinal)
                + LaunchFlag.Length + program.Length + 1;
            original = found;
            return redirected.Substring(0, at) + " " + LaunchArgsFlag + " "
                + QuoteArgument(programArguments) + redirected.Substring(at);
        }

        /// <summary>
        /// One argument quoted for a Windows command line, so CommandLineToArgvW and the C
        /// runtime both read it back as exactly <paramref name="value"/>.
        ///
        /// The rule that makes this more than wrapping in quotes: backslashes are literal
        /// except in front of a quote, where 2n of them mean n backslashes and 2n+1 mean n
        /// backslashes and a literal quote. The game itself relies on that - its --ignorePath
        /// is "Updater\1.5.0\\" - and so does this, because the value it quotes is a quoted
        /// path: pythonw has to receive the script as one argument even when the user's
        /// profile folder has a space in it.
        /// </summary>
        public static string QuoteArgument(string value)
        {
            var sb = new StringBuilder(value.Length + 8);
            sb.Append('"');
            var backslashes = 0;
            foreach (var c in value)
            {
                if (c == '\\')
                {
                    backslashes++;
                    continue;
                }
                if (c == '"')
                {
                    sb.Append('\\', backslashes * 2 + 1);
                }
                else
                {
                    sb.Append('\\', backslashes);
                }
                sb.Append(c);
                backslashes = 0;
            }
            sb.Append('\\', backslashes * 2);
            sb.Append('"');
            return sb.ToString();
        }

        /// <summary>
        /// The Windows relauncher recorded in <paramref name="dataDir"/>, as the program to
        /// run and the arguments to run it with - or null, with <paramref name="why"/> saying
        /// what was wrong, if there is not one this can safely hand an update to.
        ///
        /// Everything named has to exist right now. The updater runs whatever it is given and
        /// only then quits, so a program that is not there means an update that ends with the
        /// game closed - far worse than an update that ends with the game open without the
        /// tracker, which the watcher fixes once the game is closed.
        /// </summary>
        public static string ReadWindowsRelauncher(string dataDir, out string programArguments,
            out string why)
        {
            programArguments = null;
            why = null;

            var path = Path.Combine(dataDir, WindowsRelauncherFile);
            if (!File.Exists(path))
            {
                why = "no relauncher recorded at " + path;
                return null;
            }

            var lines = File.ReadAllLines(path, Encoding.UTF8);
            var program = lines.Length > 0 ? lines[0].Trim() : "";
            var script = lines.Length > 1 ? lines[1].Trim() : "";
            if (program.Length == 0 || script.Length == 0)
            {
                why = path + " does not name both an interpreter and a script";
                return null;
            }
            if (program.IndexOf('"') >= 0 || script.IndexOf('"') >= 0)
            {
                why = path + " names a path that cannot be quoted safely";
                return null;
            }
            if (!Path.IsPathRooted(program) || !Path.IsPathRooted(script))
            {
                why = path + " names a relative path, and the updater's working directory is "
                    + "not this one";
                return null;
            }
            if (!File.Exists(program))
            {
                why = "the relauncher's interpreter is gone: " + program;
                return null;
            }
            if (!File.Exists(script))
            {
                why = "the relauncher's script is gone: " + script;
                return null;
            }

            programArguments = "\"" + script + "\"";
            return program;
        }
    }
}
