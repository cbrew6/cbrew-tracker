using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using CbrewTracker.Hooks;

/// <summary>
/// Plain asserts and a non-zero exit on failure; no test framework, so this needs nothing
/// restored beyond what the mod itself builds with.
/// </summary>
internal static class Program
{
    private static int _passed;
    private static readonly List<string> Failures = new List<string>();

    private static int Main()
    {
        WindowsUpdaterPath();
        MacUpdaterPathUnchanged();
        Quoting();
        QuotingRoundTripsThroughWindows();
        MacRedirectUnchanged();
        WindowsRedirect();
        WindowsRedirectRefusals();
        WindowsRelauncherFile();

        Console.WriteLine();
        foreach (var f in Failures) Console.WriteLine("FAIL " + f);
        Console.WriteLine($"{_passed} passed, {Failures.Count} failed");
        return Failures.Count == 0 ? 0 : 1;
    }

    // -----------------------------------------------------------------------------------
    // The exact strings the game builds, from StartupScreenText.OpenAppUpdater on 1.42.2
    // (Windows): Path.GetFullPath(dataPath + "/../Updater/" + v + "/Updater.exe"), and an
    // argument string ending --launchAppAt "<exe>" --cleanup <n>.
    // -----------------------------------------------------------------------------------
    private const string Game = @"C:\Users\Jöhn Smith\The Pokémon Company International\Pokémon Trading Card Game Live";
    private static readonly string UpdaterExe = Game + @"\Updater\1.5.0\Updater.exe";
    private static readonly string GameExe = Game + @"\Pokemon TCG Live.exe";

    private static string GameArguments(string launchAppAt = null)
    {
        return "--manifestUrl \"https://example.invalid/updater/manifest.json\" --locale en"
            + " --supportUrl \"https://support.example.invalid/\""
            + " --installPath \"" + Game + "\""
            + " --ignorePath \"" + @"Updater\1.5.0" + "\\\\" + "\""
            + " --launchAppAt \"" + (launchAppAt ?? GameExe) + "\""
            + " --cleanup 0";
    }

    private static void WindowsUpdaterPath()
    {
        Check("windows: the game's own updater path", UpdaterHandoff.IsWindowsUpdaterLaunch(UpdaterExe));
        Check("windows: forward slashes", UpdaterHandoff.IsWindowsUpdaterLaunch(UpdaterExe.Replace('\\', '/')));
        Check("windows: mixed separators, as Path.Combine leaves them",
            UpdaterHandoff.IsWindowsUpdaterLaunch(Game.Replace('\\', '/') + @"\Updater/1.6.0/Updater.exe"));
        Check("windows: case is the file system's business",
            UpdaterHandoff.IsWindowsUpdaterLaunch(Game + @"\updater\1.5.0\UPDATER.EXE"));
        Check("windows: not the game itself", !UpdaterHandoff.IsWindowsUpdaterLaunch(GameExe));
        Check("windows: an Updater.exe with no version folder",
            !UpdaterHandoff.IsWindowsUpdaterLaunch(Game + @"\Updater.exe"));
        Check("windows: an Updater.exe not under Updater\\",
            !UpdaterHandoff.IsWindowsUpdaterLaunch(Game + @"\Tools\1.5.0\Updater.exe"));
        Check("windows: an empty version folder",
            !UpdaterHandoff.IsWindowsUpdaterLaunch(Game + @"\Updater\\Updater.exe"));
        Check("windows: a root-level C:\\Updater\\Updater.exe",
            !UpdaterHandoff.IsWindowsUpdaterLaunch(@"C:\Updater\Updater.exe"));
        Check("windows: the macOS updater",
            !UpdaterHandoff.IsWindowsUpdaterLaunch("/Applications/Pokemon TCG Live.app/Contents/Resources/Updater/1.5.0/Updater.app/Contents/MacOS/Updater"));
        Check("windows: null", !UpdaterHandoff.IsWindowsUpdaterLaunch(null));
        Check("windows: empty", !UpdaterHandoff.IsWindowsUpdaterLaunch(""));
        Check("windows: a bare Updater.exe", !UpdaterHandoff.IsWindowsUpdaterLaunch("Updater.exe"));
    }

    private static void MacUpdaterPathUnchanged()
    {
        Check("mac: the updater",
            UpdaterHandoff.IsUpdaterLaunch("/Applications/Pokemon TCG Live.app/Contents/Resources/Updater/1.5.0/Updater.app/Contents/MacOS/Updater"));
        Check("mac: never a Windows path", !UpdaterHandoff.IsUpdaterLaunch(UpdaterExe));
    }

    private static void Quoting()
    {
        Equal("quote: plain", "\"abc\"", UpdaterHandoff.QuoteArgument("abc"));
        Equal("quote: a space", "\"a b\"", UpdaterHandoff.QuoteArgument("a b"));
        Equal("quote: empty", "\"\"", UpdaterHandoff.QuoteArgument(""));
        Equal("quote: a quoted path",
            "\"\\\"C:\\Users\\Jöhn Smith\\relaunch.py\\\"\"",
            UpdaterHandoff.QuoteArgument("\"C:\\Users\\Jöhn Smith\\relaunch.py\""));
        Equal("quote: trailing backslash is doubled", "\"C:\\dir\\\\\"",
            UpdaterHandoff.QuoteArgument("C:\\dir\\"));
        Equal("quote: backslashes before a quote", "\"a\\\\\\\"b\"",
            UpdaterHandoff.QuoteArgument("a\\\"b"));
        Equal("quote: backslashes mid-string are left alone", "\"a\\\\b\"",
            UpdaterHandoff.QuoteArgument("a\\\\b"));
    }

    private static void QuotingRoundTripsThroughWindows()
    {
        var samples = new List<string>
        {
            "", " ", "abc", "a b", "\"", "\"\"", "\\", "\\\\", "a\\", "a\\\\", "\\\"", "a\\\"b",
            "\"C:\\Users\\Jöhn Smith\\.ptcgl-tracker\\relaunch.py\"",
            "C:\\Program Files\\x\\", "tab\there", "--launchAppAt \"x\"",
        };
        var random = new Random(1234);
        const string alphabet = "ab \\\"\tç";
        for (var i = 0; i < 2000; i++)
        {
            var sb = new StringBuilder();
            var len = random.Next(0, 12);
            for (var j = 0; j < len; j++) sb.Append(alphabet[random.Next(alphabet.Length)]);
            samples.Add(sb.ToString());
        }

        var bad = 0;
        foreach (var s in samples)
        {
            var argv = CommandLineToArgv("prog.exe " + UpdaterHandoff.QuoteArgument(s));
            if (argv.Length != 2 || argv[1] != s)
            {
                if (bad++ < 5)
                    Failures.Add("quote round-trip: " + Show(s) + " came back as "
                        + string.Join(" | ", Array.ConvertAll(argv, Show)));
            }
        }
        if (bad == 0) _passed++;
        Console.WriteLine($"quote round-trip: {samples.Count} strings through CommandLineToArgvW, {bad} wrong");
    }

    private static void MacRedirectUnchanged()
    {
        const string app = "/Applications/Pokemon TCG Live.app/";
        const string relauncher = "/Users/x/.ptcgl-tracker/cbrew Tracker.app";
        var args = "--manifestUrl \"https://e.invalid/m.json\" --locale en --installPath \"" + app
            + "\" --ignorePath \"Contents/Resources/Updater/\" --launchAppAt \"" + app + "\"";
        string original;
        var got = UpdaterHandoff.Redirect(args, relauncher, out original);
        Equal("mac: --launchAppAt moves", args.Replace("--launchAppAt \"" + app, "--launchAppAt \"" + relauncher), got);
        Equal("mac: the original is kept", app, original);
        Check("mac: --installPath does not move", got.Contains("--installPath \"" + app + "\""));
    }

    private static void WindowsRedirect()
    {
        const string program = @"C:\Users\Jöhn Smith\AppData\Local\Programs\Python\Python312\pythonw.exe";
        const string script = @"C:\Users\Jöhn Smith\Documents\ptcgl tracker\scripts\relaunch.py";
        var args = GameArguments();

        string original;
        var got = UpdaterHandoff.RedirectToProgram(args, program, "\"" + script + "\"", out original);
        Check("windows: redirected", got != null);
        if (got == null) return;
        Equal("windows: the original is what the game would have opened", GameExe, original);
        Console.WriteLine("windows: updater arguments become\n    " + got);

        // Parse it the way the updater does: CommandLineToArgvW for argv, then
        // TPCI.Updater.Utility.CommandLineArgs for flags - copied from the decompiled updater.
        var parsed = UpdaterFlags(CommandLineToArgv("\"" + UpdaterExe + "\" " + got));
        Equal("windows: --launchAppAt is the interpreter", program, Get(parsed, "--launchAppAt"));
        Equal("windows: --launchAppWithArgs is the quoted script", "\"" + script + "\"",
            Get(parsed, "--launchAppWithArgs"));
        Equal("windows: --installPath does not move", Game, Get(parsed, "--installPath"));
        Equal("windows: --ignorePath keeps its trailing backslash", @"Updater\1.5.0\", Get(parsed, "--ignorePath"));
        Equal("windows: --cleanup survives", "0", Get(parsed, "--cleanup"));
        Equal("windows: --manifestUrl survives", "https://example.invalid/updater/manifest.json",
            Get(parsed, "--manifestUrl"));
        Equal("windows: --locale survives", "en", Get(parsed, "--locale"));

        // And what the updater then does with it: Process.Start(launchAppAt, launchAppWithArgs),
        // which is pythonw "<script>" - the script must arrive as one argument.
        var child = CommandLineToArgv("\"" + program + "\" " + Get(parsed, "--launchAppWithArgs"));
        Check("windows: pythonw receives the script as a single argument",
            child.Length == 2 && child[1] == script);

        // The flags as the game sent them, before: nothing else changed.
        var before = UpdaterFlags(CommandLineToArgv("\"" + UpdaterExe + "\" " + args));
        foreach (var kv in before)
        {
            if (kv.Key == "--launchAppAt") continue;
            Equal("windows: " + kv.Key + " identical to the game's", kv.Value, Get(parsed, kv.Key));
        }
        Equal("windows: exactly one flag added", before.Count + 1, parsed.Count);
    }

    private static void WindowsRedirectRefusals()
    {
        const string program = @"C:\py\pythonw.exe";
        const string scriptArg = "\"C:\\t\\relaunch.py\"";
        string original;

        Check("refuse: the game already passes --launchAppWithArgs",
            UpdaterHandoff.RedirectToProgram(GameArguments() + " --launchAppWithArgs \"-x\"", program, scriptArg, out original) == null
            && original == null);
        Check("refuse: no --launchAppAt",
            UpdaterHandoff.RedirectToProgram("--installPath \"C:\\g\"", program, scriptArg, out original) == null);
        Check("refuse: two --launchAppAt",
            UpdaterHandoff.RedirectToProgram(GameArguments() + " --launchAppAt \"C:\\x.exe\"", program, scriptArg, out original) == null);
        Check("refuse: an unterminated --launchAppAt",
            UpdaterHandoff.RedirectToProgram("--launchAppAt \"C:\\g\\x.exe", program, scriptArg, out original) == null);
        Check("refuse: an empty --launchAppAt",
            UpdaterHandoff.RedirectToProgram("--launchAppAt \"\" --cleanup 0", program, scriptArg, out original) == null);
        Check("refuse: a program with a quote in it",
            UpdaterHandoff.RedirectToProgram(GameArguments(), "C:\\a\"b.exe", scriptArg, out original) == null);
        Check("refuse: no program arguments",
            UpdaterHandoff.RedirectToProgram(GameArguments(), program, "", out original) == null);
        Check("refuse: null arguments",
            UpdaterHandoff.RedirectToProgram(null, program, scriptArg, out original) == null);
        Check("refuse: already pointing at the program",
            UpdaterHandoff.RedirectToProgram(GameArguments(program), program, scriptArg, out original) == null);
    }

    private static void WindowsRelauncherFile()
    {
        var dir = Path.Combine(Path.GetTempPath(), "ptcgl-handoff-test-" + Guid.NewGuid().ToString("N"));
        var spaced = Path.Combine(dir, "Jöhn Smith");
        Directory.CreateDirectory(spaced);
        var program = Path.Combine(spaced, "pythonw.exe");
        var script = Path.Combine(spaced, "relaunch.py");
        File.WriteAllText(program, "");
        File.WriteAllText(script, "");
        var file = Path.Combine(dir, UpdaterHandoff.WindowsRelauncherFile);
        try
        {
            string args, why;

            Check("relauncher: none recorded",
                UpdaterHandoff.ReadWindowsRelauncher(dir, out args, out why) == null && why.Contains("no relauncher"));

            // PowerShell 5.1 writes a BOM unless told otherwise, and a hand-edited file will
            // have CRLF and stray spaces. None of that may matter.
            File.WriteAllText(file, program + "  \r\n" + script + "\r\n", new UTF8Encoding(true));
            var got = UpdaterHandoff.ReadWindowsRelauncher(dir, out args, out why);
            Equal("relauncher: the interpreter, despite BOM/CRLF/spaces", program, got);
            Equal("relauncher: the script, quoted", "\"" + script + "\"", args);

            File.WriteAllText(file, program + "\n");
            Check("relauncher: only one line",
                UpdaterHandoff.ReadWindowsRelauncher(dir, out args, out why) == null && why.Contains("both"));

            File.WriteAllText(file, "pythonw.exe\n" + script + "\n");
            Check("relauncher: a relative interpreter",
                UpdaterHandoff.ReadWindowsRelauncher(dir, out args, out why) == null && why.Contains("relative"));

            File.WriteAllText(file, program + "x\n" + script + "\n");
            Check("relauncher: the interpreter is gone",
                UpdaterHandoff.ReadWindowsRelauncher(dir, out args, out why) == null && why.Contains("interpreter is gone"));

            File.WriteAllText(file, program + "\n" + script + "x\n");
            Check("relauncher: the script is gone",
                UpdaterHandoff.ReadWindowsRelauncher(dir, out args, out why) == null && why.Contains("script is gone"));

            File.WriteAllText(file, program + "\n\"" + script + "\"\n");
            Check("relauncher: a quote in a path",
                UpdaterHandoff.ReadWindowsRelauncher(dir, out args, out why) == null && why.Contains("quoted"));

            File.WriteAllText(file, "");
            Check("relauncher: an empty file",
                UpdaterHandoff.ReadWindowsRelauncher(dir, out args, out why) == null);
        }
        finally
        {
            try { Directory.Delete(dir, true); } catch { }
        }
    }

    // -----------------------------------------------------------------------------------
    // helpers
    // -----------------------------------------------------------------------------------

    /// <summary>TPCI.Updater.Utility.CommandLineArgs, as decompiled from Updater 1.5.0.</summary>
    private static Dictionary<string, string> UpdaterFlags(string[] argv)
    {
        var d = new Dictionary<string, string>();
        for (var i = 0; i < argv.Length; i++)
        {
            if (!argv[i].StartsWith("--")) continue;
            var key = argv[i];
            if (!d.ContainsKey(key)) d[key] = string.Empty;
            if (i + 1 < argv.Length && !argv[i + 1].StartsWith("--")) d[key] = argv[++i];
        }
        return d;
    }

    private static string Get(Dictionary<string, string> d, string key)
    {
        string v;
        return d.TryGetValue(key, out v) ? v : "<absent>";
    }

    [DllImport("shell32.dll", SetLastError = true)]
    private static extern IntPtr CommandLineToArgvW([MarshalAs(UnmanagedType.LPWStr)] string cmdLine, out int numArgs);

    [DllImport("kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr mem);

    private static string[] CommandLineToArgv(string commandLine)
    {
        int n;
        var ptr = CommandLineToArgvW(commandLine, out n);
        if (ptr == IntPtr.Zero) throw new InvalidOperationException("CommandLineToArgvW failed");
        try
        {
            var result = new string[n];
            for (var i = 0; i < n; i++)
                result[i] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(ptr, i * IntPtr.Size));
            return result;
        }
        finally
        {
            LocalFree(ptr);
        }
    }

    private static string Show(string s) => "[" + s.Replace("\t", "\\t") + "]";

    private static void Check(string name, bool ok)
    {
        if (ok) _passed++;
        else Failures.Add(name);
    }

    private static void Equal<T>(string name, T expected, T actual)
    {
        if (EqualityComparer<T>.Default.Equals(expected, actual)) _passed++;
        else Failures.Add(name + "\n      expected " + expected + "\n      actual   " + actual);
    }
}
