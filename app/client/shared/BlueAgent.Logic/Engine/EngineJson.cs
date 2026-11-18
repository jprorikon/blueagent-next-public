using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace BlueAgent.Logic.Engine
{
    /// <summary>
    /// **有序**的 JSON 物件（對應 Kotlin 的 `LinkedHashMap`）。
    ///
    /// 為什麼不用 `Dictionary`：這一層的**鍵序是資料的一部分**——`ModelProfile` 的通道順序決定
    /// `ModelProfileAdapter` 的「後寫覆蓋先寫」與 `deriveNeutralParams` 的「先到先寫」。
    /// .NET 的 `Dictionary` 雖然實務上照插入序列舉，但那是實作細節，這裡不押它。
    /// 同鍵覆蓋時**保留原本的位置**（與 `LinkedHashMap.put` 同）。
    /// </summary>
    public sealed class JsonObject
    {
        private readonly List<KeyValuePair<string, object>> _items = new List<KeyValuePair<string, object>>();
        private readonly Dictionary<string, int> _index = new Dictionary<string, int>();

        public int Count => _items.Count;

        public IEnumerable<KeyValuePair<string, object>> Items => _items;

        public IEnumerable<string> Keys
        {
            get
            {
                foreach (var item in _items) yield return item.Key;
            }
        }

        public void Add(string key, object value)
        {
            if (_index.TryGetValue(key, out var at))
            {
                _items[at] = new KeyValuePair<string, object>(key, value);
                return;
            }
            _index[key] = _items.Count;
            _items.Add(new KeyValuePair<string, object>(key, value));
        }

        public bool TryGetValue(string key, out object value)
        {
            if (key != null && _index.TryGetValue(key, out var at))
            {
                value = _items[at].Value;
                return true;
            }
            value = null;
            return false;
        }

        /// <summary>取值：缺席回 null（對應 Kotlin `map[key]`）。</summary>
        public object this[string key] => TryGetValue(key, out var v) ? v : null;
    }

    /// <summary>
    /// 極簡 JSON 讀取器 —— C# 版，對照 Kotlin `Json.kt`。
    ///
    /// 為什麼不用 `System.Text.Json`：`ModelProfile.FromJson` 是**引擎語意**的一部分
    /// （預設值、`target`／`targets` 兩種寫法、`idleConfig` 的兩元素陣列），而且這裡刻意
    /// 與 Kotlin 那支**逐行同構**——連「不認得的寫法寧可早爆、不要安靜地讀錯」也一樣。
    /// fixture 的 `jsonCases` 就是拿來對拍這件事的（含 12 個應該丟例外的輸入）。
    /// </summary>
    public static class EngineJson
    {
        public static object Parse(string text)
        {
            var p = new Parser(text);
            p.Ws();
            var v = p.Value();
            p.Ws();
            if (p.I < p.S.Length) throw new JsonParseException($"JSON 尾端有多餘內容 @{p.I}");
            return v;
        }

        /// <summary>不是物件 ⇒ 空物件（對應 Kotlin `Json.obj` 的 `?: emptyMap()`）。</summary>
        public static JsonObject Obj(object v) => v as JsonObject ?? new JsonObject();

        /// <summary>不是陣列 ⇒ 空陣列。</summary>
        public static List<object> Arr(object v) => v as List<object> ?? new List<object>();

        public static double Num(object v, double d = 0.0)
        {
            if (v is double dv) return dv;
            if (v is float fv) return fv;
            if (v is int iv) return iv;
            if (v is long lv) return lv;
            return d;
        }

        public static float F(object v, float d = 0f) => (float)Num(v, d);

        public static string Str(object v, string d = "") => v as string ?? d;

        public static bool Bool(object v, bool d = false) => v is bool b ? b : d;

        /// <summary>Kotlin 的 `Char.isWhitespace()`：**不含** NBSP／FIGURE SPACE／NARROW NBSP（C# 的 `char.IsWhiteSpace` 含）。</summary>
        internal static bool IsWhitespace(char c) =>
            c == '\t' || c == '\n' || c == '\u000B' || c == '\f' || c == '\r' ||
            (c >= '\u001C' && c <= '\u001F') ||
            (char.IsWhiteSpace(c) && c != '\u00A0' && c != '\u2007' && c != '\u202F');

        private sealed class Parser
        {
            public readonly string S;
            public int I;

            public Parser(string s) => S = s;

            public void Ws()
            {
                while (I < S.Length && IsWhitespace(S[I])) I++;
            }

            private JsonParseException Error(string msg) => new JsonParseException($"JSON: {msg} @{I}");

            public object Value()
            {
                Ws();
                if (I >= S.Length) throw Error("意外結束");
                var c = S[I];
                switch (c)
                {
                    case '{': return ObjValue();
                    case '[': return ArrValue();
                    case '"': return StrValue();
                    case 't': return Lit("true", true);
                    case 'f': return Lit("false", false);
                    case 'n': return Lit("null", null);
                    default:
                        if (c == '-' || (c >= '0' && c <= '9')) return Number();
                        throw Error($"不認得的字元 '{c}'");
                }
            }

            private object Lit(string word, object v)
            {
                if (!S.Substring(I).StartsWith(word)) throw Error($"不是 {word}");
                I += word.Length;
                return v;
            }

            private JsonObject ObjValue()
            {
                var outp = new JsonObject();
                I++; // {
                Ws();
                if (I < S.Length && S[I] == '}')
                {
                    I++;
                    return outp;
                }
                while (true)
                {
                    Ws();
                    var k = StrValue();
                    Ws();
                    if (I >= S.Length || S[I] != ':') throw Error("物件缺 ':'");
                    I++;
                    outp.Add(k, Value());
                    Ws();
                    if (I >= S.Length) throw Error("物件沒收尾");
                    var c = S[I];
                    if (c == ',') { I++; continue; }
                    if (c == '}') { I++; return outp; }
                    throw Error("物件裡預期 ',' 或 '}'");
                }
            }

            private List<object> ArrValue()
            {
                var outp = new List<object>();
                I++; // [
                Ws();
                if (I < S.Length && S[I] == ']')
                {
                    I++;
                    return outp;
                }
                while (true)
                {
                    outp.Add(Value());
                    Ws();
                    if (I >= S.Length) throw Error("陣列沒收尾");
                    var c = S[I];
                    if (c == ',') { I++; continue; }
                    if (c == ']') { I++; return outp; }
                    throw Error("陣列裡預期 ',' 或 ']'");
                }
            }

            private string StrValue()
            {
                if (I >= S.Length || S[I] != '"') throw Error("預期字串");
                I++;
                var sb = new StringBuilder();
                while (I < S.Length)
                {
                    var c = S[I];
                    if (c == '"')
                    {
                        I++;
                        return sb.ToString();
                    }
                    if (c == '\\')
                    {
                        I++;
                        if (I >= S.Length) throw Error("字串沒收尾");
                        var e = S[I];
                        if (e == '"') sb.Append('"');
                        else if (e == '\\') sb.Append('\\');
                        else if (e == '/') sb.Append('/');
                        else if (e == 'b') sb.Append('\b');
                        else if (e == 'f') sb.Append('\u000C');
                        else if (e == 'n') sb.Append('\n');
                        else if (e == 'r') sb.Append('\r');
                        else if (e == 't') sb.Append('\t');
                        else if (e == 'u')
                        {
                            if (I + 4 >= S.Length) throw Error("\\u 少了四位");
                            var hex = S.Substring(I + 1, 4);
                            if (!ushort.TryParse(hex, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var code))
                                throw Error($"\\u 不是十六進位：{hex}");
                            sb.Append((char)code);
                            I += 4;
                        }
                        else throw Error($"不認得的轉義 \\{e}");
                        I++;
                        continue;
                    }
                    sb.Append(c);
                    I++;
                }
                throw Error("字串沒收尾");
            }

            private double Number()
            {
                var start = I;
                if (I < S.Length && S[I] == '-') I++;
                while (I < S.Length)
                {
                    var c = S[I];
                    if ((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') I++;
                    else break;
                }
                var t = S.Substring(start, I - start);
                if (double.TryParse(t, NumberStyles.Float, CultureInfo.InvariantCulture, out var value)) return value;
                throw Error($"不是數字：{t}");
            }
        }
    }

    /// <summary>JSON 讀取失敗（對應 Kotlin 的 `IllegalArgumentException`）。</summary>
    public sealed class JsonParseException : System.Exception
    {
        public JsonParseException(string message) : base(message) { }
    }
}
