using System;
using System.Linq;
using System.Reflection;
using CbrewTracker.Emit;

namespace CbrewTracker.Discovery
{
    /// <summary>
    /// Diagnostics for when a game update moves a hook target. Turns "the tracker silently
    /// stopped recording" into a log line naming what the members are called now.
    /// </summary>
    internal static class SymbolProbe
    {
        /// <summary>Logs every member of <paramref name="type"/> whose name contains <paramref name="needle"/>.</summary>
        public static void DumpMembers(Type type, string needle)
        {
            if (type == null)
            {
                Log.Warn($"SymbolProbe: type not found (looking for '{needle}')");
                return;
            }

            const BindingFlags All = BindingFlags.Public | BindingFlags.NonPublic
                                   | BindingFlags.Instance | BindingFlags.Static;

            Log.Info($"SymbolProbe: members of {type.FullName} matching '{needle}':");

            foreach (var m in type.GetMethods(All).Select(m => m.Name).Distinct().OrderBy(n => n))
                if (Contains(m, needle)) Log.Info($"    method {m}");

            foreach (var f in type.GetFields(All))
                if (Contains(f.Name, needle)) Log.Info($"    field  {f.FieldType.Name} {f.Name}");

            foreach (var p in type.GetProperties(All))
                if (Contains(p.Name, needle)) Log.Info($"    prop   {p.PropertyType.Name} {p.Name}");
        }

        /// <summary>
        /// Scans every loaded assembly for types whose name contains <paramref name="needle"/>.
        /// The fallback when a whole type has been renamed rather than one of its members.
        /// </summary>
        public static void FindTypes(string needle)
        {
            Log.Info($"SymbolProbe: loaded types matching '{needle}':");

            foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type[] types;
                try
                {
                    types = asm.GetTypes();
                }
                catch (ReflectionTypeLoadException ex)
                {
                    types = ex.Types.Where(t => t != null).ToArray();
                }
                catch
                {
                    continue;   // assembly refuses to enumerate; nothing useful here
                }

                foreach (var t in types)
                    if (t.Name != null && Contains(t.Name, needle))
                        Log.Info($"    {asm.GetName().Name}: {t.FullName}");
            }
        }

        private static bool Contains(string haystack, string needle) =>
            haystack != null && haystack.IndexOf(needle, StringComparison.OrdinalIgnoreCase) >= 0;
    }
}
