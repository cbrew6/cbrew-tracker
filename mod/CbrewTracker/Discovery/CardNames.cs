using System;
using System.Collections.Generic;
using System.Reflection;
using CbrewTracker.Emit;

namespace CbrewTracker.Discovery
{
    /// <summary>
    /// Resolves PTCGL card ids ("sv10_164") to English card names, using the game's own
    /// loaded card database.
    ///
    /// The database ships as a base64 binary table in the config cache. Rather than decode
    /// that format ourselves — it has framing we did not crack, and it would drift with the
    /// game — we ask the client, which already has it parsed:
    ///
    ///     ManagerSingleton&lt;CardDatabaseManager&gt;.instance.cardDatabase
    ///         .TryGetCardDataRow(id, out row) -> row.EnglishCardName
    ///
    /// Reached by reflection on purpose. The chain crosses several assemblies and a generic
    /// singleton base, and the tracker is expensive to iterate on (rebuild, reinstall,
    /// replay). Reflection degrades to "no name" instead of refusing to compile or throwing
    /// mid-match, and the raw id is always kept regardless.
    /// </summary>
    internal static class CardNames
    {
        private static readonly Dictionary<string, string> Cache = new Dictionary<string, string>();
        private static readonly Dictionary<string, string> CategoryCache = new Dictionary<string, string>();

        private static object _database;
        private static MethodInfo _tryGet;
        private static PropertyInfo _englishName;
        private static bool _resolved;
        private static bool _reportedFailure;

        // The game's own card categoriser, reached the same way as the name: extension
        // methods on the card-data row. GetCardCategory -> Pokemon/Trainer/Energy, and for
        // a Trainer, GetTrainerType -> ITEM/TOOL/STADIUM/SUPPORT. This is what lets anything
        // downstream sort a decklist into sections offline, with no card-API round trip -
        // the section ships with the match.
        private static MethodInfo _getCategory;
        private static MethodInfo _getTrainerType;
        private static bool _categoryResolved;
        private static bool _reportedCategoryFailure;

        /// <summary>English name for a card id, or null if it cannot be resolved.</summary>
        public static string Get(string cardId)
        {
            if (string.IsNullOrEmpty(cardId)) return null;
            if (Cache.TryGetValue(cardId, out var cached)) return cached;

            string name = null;
            try
            {
                var row = GetRow(cardId);
                if (row != null)
                {
                    if (_englishName == null)
                        _englishName = row.GetType().GetProperty("EnglishCardName");
                    name = _englishName?.GetValue(row) as string;
                }
            }
            catch (Exception ex)
            {
                if (!_reportedFailure)
                {
                    _reportedFailure = true;
                    Log.Warn($"CardNames: lookup failed, decklists keep raw ids ({ex.GetType().Name})");
                }
            }

            Cache[cardId] = name;
            return name;
        }

        /// <summary>
        /// Deck section for a card id — "Pokemon", "Item", "Supporter", "Stadium", "Tool",
        /// "Energy", or null if it cannot be resolved. Read straight from the game database,
        /// so nothing downstream needs a card API to sort a decklist.
        /// </summary>
        public static string GetCategory(string cardId)
        {
            if (string.IsNullOrEmpty(cardId)) return null;
            if (CategoryCache.TryGetValue(cardId, out var cached)) return cached;

            string section = null;
            try
            {
                var row = GetRow(cardId);
                if (row != null && EnsureCategory(row.GetType()))
                {
                    var category = _getCategory.Invoke(null, new[] { row })?.ToString();
                    if (category == "Trainer")
                    {
                        // An unknown subtype stays "Trainer" rather than being given a
                        // section that might be wrong.
                        section = TrainerSection(_getTrainerType?.Invoke(null, new[] { row }))
                                  ?? "Trainer";
                    }
                    else if (category == "Pokemon") section = "Pokemon";
                    else if (category == "Energy")  section = "Energy";
                }
            }
            catch (Exception ex)
            {
                if (!_reportedCategoryFailure)
                {
                    _reportedCategoryFailure = true;
                    Log.Warn($"CardNames: category lookup failed, decklists carry no deck "
                             + $"section ({ex.GetType().Name})");
                }
            }

            CategoryCache[cardId] = section;
            return section;
        }

        /// <summary>
        /// The deck section for a trainer, from GetTrainerType's return — or null when the
        /// value is unrecognised (caller then keeps a generic "Trainer").
        ///
        /// GetTrainerType is typed to return a bare int, and — verified against real decklists
        /// cross-checked with the public card database — its scheme is Item=0, Stadium=1,
        /// Supporter=2, Tool=3. That is deliberately hand-mapped, not run through Enum.GetName:
        /// the int is NOT the TrainerType enum's own numbering (that enum is ITEM=0, TOOL=1,
        /// STADIUM=2, SUPPORT=3), and no enum in the game carries this scheme, so translating
        /// through any of them mislabels every card. Item/Stadium/Supporter were each confirmed
        /// against the card database; Tool is the one remaining slot. A value outside 0..3
        /// returns null so a game update that reshuffles the scheme degrades to "Trainer"
        /// rather than sorting silently wrong.
        /// </summary>
        private static string TrainerSection(object raw)
        {
            if (raw == null) return null;
            try
            {
                switch (Convert.ToInt32(raw))
                {
                    case 0: return "Item";
                    case 1: return "Stadium";
                    case 2: return "Supporter";
                    case 3: return "Tool";
                }
            }
            catch { /* not an int we can read; fall through to the generic fallback */ }
            return null;
        }

        /// <summary>The card-data row for an id, or null. Both Get and GetCategory use it.</summary>
        private static object GetRow(string cardId)
        {
            if (!EnsureDatabase()) return null;
            if (_tryGet.GetParameters().Length == 2)
            {
                // TryGetCardDataRow(string, out ICardDataRow)
                var args = new object[] { cardId, null };
                return _tryGet.Invoke(_database, args) is bool ok && ok ? args[1] : null;
            }
            // GetCardById(string) -> ICardDataRow
            return _tryGet.Invoke(_database, new object[] { cardId });
        }

        /// <summary>Bind the GetCardCategory / GetTrainerType extension methods once.</summary>
        private static bool EnsureCategory(Type rowType)
        {
            if (_categoryResolved) return _getCategory != null;
            _categoryResolved = true;

            var ext = FindType("CardDataRowExtensions");
            if (ext == null)
            {
                Log.Warn("CardNames: CardDataRowExtensions not found; decklists carry no deck section");
                return false;
            }
            foreach (var m in ext.GetMethods(BindingFlags.Public | BindingFlags.Static))
            {
                var ps = m.GetParameters();
                if (ps.Length != 1 || !ps[0].ParameterType.IsAssignableFrom(rowType)) continue;
                if (m.Name == "GetCardCategory") _getCategory = m;
                else if (m.Name == "GetTrainerType") _getTrainerType = m;
            }
            if (_getCategory == null)
            {
                Log.Warn("CardNames: GetCardCategory not found; decklists carry no deck section");
                return false;
            }
            if (_getTrainerType == null)
                Log.Warn("CardNames: GetTrainerType not found; trainers are left as \"Trainer\"");

            Log.Info("CardNames: card categories ready; decklists will carry their deck section");
            return true;
        }

        private static bool EnsureDatabase()
        {
            if (_resolved) return _database != null && _tryGet != null;
            _resolved = true;

            var managerType = FindType("CardDatabaseManager");
            if (managerType == null)
            {
                Log.Warn("CardNames: CardDatabaseManager not found; decklists keep raw ids");
                return false;
            }

            // ManagerSingleton<T>.instance — a static property on the generic base.
            object manager = null;
            for (var t = managerType; t != null; t = t.BaseType)
            {
                var prop = t.GetProperty("instance",
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
                if (prop != null)
                {
                    manager = prop.GetValue(null);
                    if (manager != null) break;
                }
            }
            if (manager == null)
            {
                Log.Warn("CardNames: no live CardDatabaseManager instance; decklists keep raw ids");
                return false;
            }

            _database = managerType.GetProperty("cardDatabase")?.GetValue(manager);
            if (_database == null)
            {
                Log.Warn("CardNames: card database not initialised yet; decklists keep raw ids");
                return false;
            }

            // Current game builds expose GetCardById(string); some expose
            // TryGetCardDataRow(string, out row). Accept either shape.
            foreach (var m in _database.GetType().GetMethods())
            {
                var ps = m.GetParameters();
                if (m.Name == "GetCardById" && ps.Length == 1 && ps[0].ParameterType == typeof(string))
                {
                    _tryGet = m;
                    break;
                }
                if (m.Name == "TryGetCardDataRow" && ps.Length == 2
                    && ps[0].ParameterType == typeof(string) && ps[1].IsOut)
                {
                    _tryGet = m;
                }
            }

            if (_tryGet == null)
            {
                Log.Warn("CardNames: no card lookup method found; decklists keep raw ids");
                SymbolProbe.DumpMembers(_database.GetType(), "Card");
                return false;
            }

            Log.Info("CardNames: card database ready; decklists will carry names");
            return true;
        }

        private static Type FindType(string name)
        {
            foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type[] types;
                try { types = asm.GetTypes(); }
                catch (ReflectionTypeLoadException ex) { types = Array.FindAll(ex.Types, t => t != null); }
                catch { continue; }

                foreach (var t in types)
                    if (t.Name == name) return t;
            }
            return null;
        }
    }
}
