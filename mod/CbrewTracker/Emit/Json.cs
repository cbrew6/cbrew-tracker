using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace CbrewTracker.Emit
{
    /// <summary>
    /// Just enough JSON to write flat event objects. Hand-rolled rather than using the
    /// game's bundled Newtonsoft so the mod does not couple to whichever version of it
    /// the client happens to ship.
    /// </summary>
    internal static class Json
    {
        /// <summary>Serialises an ordered field list to a single-line JSON object.</summary>
        public static string Object(IEnumerable<KeyValuePair<string, object>> fields)
        {
            var sb = new StringBuilder(256);
            sb.Append('{');
            var first = true;
            foreach (var kv in fields)
            {
                if (kv.Value == null) continue;   // omit rather than emit null
                if (!first) sb.Append(',');
                first = false;
                WriteString(sb, kv.Key);
                sb.Append(':');
                WriteValue(sb, kv.Value);
            }
            sb.Append('}');
            return sb.ToString();
        }

        private static void WriteValue(StringBuilder sb, object v)
        {
            switch (v)
            {
                case string s:
                    WriteString(sb, s);
                    break;
                case bool b:
                    sb.Append(b ? "true" : "false");
                    break;
                case uint u:
                    sb.Append(u.ToString(CultureInfo.InvariantCulture));
                    break;
                case int i:
                    sb.Append(i.ToString(CultureInfo.InvariantCulture));
                    break;
                case long l:
                    sb.Append(l.ToString(CultureInfo.InvariantCulture));
                    break;
                case double d:
                    sb.Append(d.ToString("R", CultureInfo.InvariantCulture));
                    break;
                default:
                    WriteString(sb, v.ToString());
                    break;
            }
        }

        /// <summary>Public so callers building nested JSON reuse the same escaping.</summary>
        public static void WriteQuoted(StringBuilder sb, string s) => WriteString(sb, s);

        private static void WriteString(StringBuilder sb, string s)
        {
            sb.Append('"');
            foreach (var c in s)
            {
                switch (c)
                {
                    case '"':  sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n");  break;
                    case '\r': sb.Append("\\r");  break;
                    case '\t': sb.Append("\\t");  break;
                    case '\b': sb.Append("\\b");  break;
                    case '\f': sb.Append("\\f");  break;
                    default:
                        // Escape control characters; pass everything else through as UTF-8.
                        if (c < 0x20)
                            sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        else
                            sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
        }
    }
}
