using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace BlueAgent.Logic
{
    /// <summary>
    /// 兩端共用的 JSON —— **一層極薄的門面，刻意長得像 `org.json`**（對照 Kotlin 版的 `Json.kt`）。
    ///
    /// 這一份是 C#／Unity 側的等價實作，目標是**與 Kotlin 版逐位元相同**：
    ///
    /// * **序列化**：緊湊（無空白）、非 ASCII **不逃脫**（日文原樣寫出）、控制字元用 `\u00xx`；
    ///   數字用 Kotlin／Java 的 `Double.toString()` 格式（整數值要帶 `.0`，`1.0E21` 這種大數走科學記號）；
    ///   解析進來的數字**保留原字面值**（`1.50` 原樣吐回，與 kotlinx 相同）。
    /// * **讀取語意**：刻意寬鬆——缺鍵／JSON null ⇒ `optString` 給 `""`、`optInt` 給 fallback、
    ///   **數字字串也算數字**（`"1"` 當 1）、布林字串也算布林（`"true"` 當 true）、
    ///   `has` 對 JSON null 回 true、**`isNull` 對「缺鍵」也回 true**（這條是為了對齊 Android 的
    ///   `org.json`，不是為了符合直覺——Kotlin 那側就是被這條咬過一次）。
    ///
    /// 為什麼要重寫而不是用 `System.Text.Json`：上面那些寬鬆語意是**行為契約**，不是實作細節；
    /// 而且 `org.json` 式的可變 `put`（`put(a,b).put(c,d)`）與**插入序**（同名鍵取代但留在原位）
    /// 都得自己維護。等價性由 fixture 對拍（`fixtures/json.json`，操作序列＋結果軌跡）釘住。
    ///
    /// ⚠️ 命名刻意沿用 Kotlin 側（`Put`／`OptString`／`IsNull`…）——移植過來的呼叫端要一眼看得懂，
    /// 名字一改就會多出一次「悄悄走鐘」的機會。
    /// </summary>
    public sealed class JObject
    {
        // 插入序 ＋ 同名鍵取代但留在原位（＝ LinkedHashMap 的語意）
        private readonly List<string> _order = new List<string>();
        private readonly Dictionary<string, JVal> _map = new Dictionary<string, JVal>();

        public JObject() { }

        /// <summary>解析字串；**壞掉的 JSON 直接丟例外**（呼叫端一律自行 try／catch，與 Kotlin 相同）。</summary>
        public JObject(string text)
        {
            var parsed = JsonCodec.Parse(text);
            if (parsed is JObjVal o)
            {
                foreach (var k in o.Value._order) { _order.Add(k); _map[k] = o.Value._map[k]; }
            }
            // 不是物件（陣列／null／純量）⇒ 空物件，與 Kotlin 的 `as? JsonObject ?: emptyMap()` 相同
        }

        internal static JObject FromOrdered(IEnumerable<KeyValuePair<string, JVal>> items)
        {
            var o = new JObject();
            foreach (var kv in items) { o._order.Add(kv.Key); o._map[kv.Key] = kv.Value; }
            return o;
        }

        // ── 寫 ──────────────────────────────────────────────────────────────
        public JObject Put(string key, string value) => Set(key, value == null ? (JVal)JNullVal.I : new JStrVal(value));
        public JObject Put(string key, int value) => Set(key, JNumVal.OfInt(value));
        public JObject Put(string key, long value) => Set(key, JNumVal.OfLong(value));
        public JObject Put(string key, double value) => Set(key, JNumVal.OfDouble(value));
        public JObject Put(string key, bool value) => Set(key, new JBoolVal(value));
        public JObject Put(string key, JObject value) => Set(key, new JObjVal(value));
        public JObject Put(string key, JArray value) => Set(key, new JArrVal(value));

        /// <summary>對照 Kotlin 的 `put(key, JNull)`（C# 沒有 `JNull` 型別，另開一支）。</summary>
        public JObject PutNull(string key) => Set(key, JNullVal.I);

        /// <summary>同名鍵＝取代（與 `org.json` 的 `put` 相同）。**內部用**：值型別不外露。</summary>
        internal JObject Set(string key, JVal value)
        {
            if (!_map.ContainsKey(key)) _order.Add(key);
            _map[key] = value;
            return this;
        }

        public void Remove(string key)
        {
            if (_map.Remove(key)) _order.Remove(key);
        }

        // ── 讀（寬鬆：缺鍵不丟例外）─────────────────────────────────────────
        /// <summary>缺鍵／JSON null ⇒ `""`；數字、布林會被轉成字串（`org.json` 的 `optString` 語意）。</summary>
        public string OptString(string key) => Primitive(key)?.Content() ?? "";

        /// <summary>缺鍵／JSON null ⇒ fallback；**空字串仍然是空字串**（不當成缺鍵）。</summary>
        public string OptString(string key, string fallback) =>
            Has(key) && !IsNull(key) ? OptString(key) : fallback;

        public int OptInt(string key, int fallback = 0)
        {
            var p = Primitive(key);
            if (p == null) return fallback;
            return p.ContentIntOrNull() ?? (double.TryParse(p.Content(), NumberStyles.Float, CultureInfo.InvariantCulture, out var d) ? JsonNumbers.SatInt(d) : fallback);
        }

        public double OptDouble(string key, double fallback = 0.0)
        {
            var p = Primitive(key);
            if (p == null) return fallback;
            return p.ContentDoubleOrNull() ?? (double.TryParse(p.Content(), NumberStyles.Float, CultureInfo.InvariantCulture, out var d) ? d : fallback);
        }

        /// <summary>缺鍵／非數字 ⇒ fallback；**數字字串也算數字**（與 `OptInt` 同一條規則）。</summary>
        public long OptLong(string key, long fallback)
        {
            var p = Primitive(key);
            if (p == null) return fallback;
            var l = p.ContentLongOrNull();
            if (l.HasValue) return l.Value;
            if (long.TryParse(p.Content(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var l2)) return l2;
            if (double.TryParse(p.Content(), NumberStyles.Float, CultureInfo.InvariantCulture, out var d)) return JsonNumbers.SatLong(d);
            return fallback;
        }

        public bool OptBoolean(string key, bool fallback = false)
        {
            var p = Primitive(key);
            if (p == null) return fallback;
            var b = p.ContentBoolOrNull();
            if (b.HasValue) return b.Value;
            switch (p.Content().ToLowerInvariant())
            {
                case "true": case "1": return true;
                case "false": case "0": return false;
                default: return fallback;
            }
        }

        public JObject OptJObject(string key) => (_map.TryGetValue(key, out var v) && v is JObjVal o) ? o.Value : null;
        public JArray OptJArray(string key) => (_map.TryGetValue(key, out var v) && v is JArrVal a) ? a.Value : null;

        /// <summary>取不到就丟（`org.json` 的 `getJSONArray` 語意；呼叫端自行 try／catch）。</summary>
        public JArray GetJSONArray(string key) => OptJArray(key) ?? throw new KeyNotFoundException("no array: " + key);
        public JObject GetJSONObject(string key) => OptJObject(key) ?? throw new KeyNotFoundException("no object: " + key);

        // ── 嚴格取值（`org.json` 的 get* 語意：缺鍵／型別不對 ⇒ 丟）────────────────────
        public string GetString(string key)
        {
            if (!Has(key) || IsNull(key)) throw new KeyNotFoundException("no value: " + key);
            return OptString(key);
        }

        public int GetInt(string key)
        {
            var i = Primitive(key)?.ContentIntOrNull();
            return i ?? throw new KeyNotFoundException("not an int: " + key);
        }

        public long GetLong(string key)
        {
            var l = Primitive(key)?.ContentLongOrNull();
            return l ?? throw new KeyNotFoundException("not a long: " + key);
        }

        public double GetDouble(string key)
        {
            var d = Primitive(key)?.ContentDoubleOrNull();
            return d ?? throw new KeyNotFoundException("not a double: " + key);
        }

        public bool GetBoolean(string key)
        {
            var b = Primitive(key)?.ContentBoolOrNull();
            return b ?? throw new KeyNotFoundException("not a boolean: " + key);
        }

        /// <summary>鍵存在就算存在（值是 JSON null 也算——與 `org.json` 的 `has` 相同）。</summary>
        public bool Has(string key) => _map.ContainsKey(key);

        /// <summary>
        /// 鍵**缺**或值**是** JSON null ⇒ true（＝ `org.json` 的 `isNull`）。
        /// ⚠️ **這一支的語意是為了對齊 Android 的 org.json，不是為了符合直覺**（缺鍵回 true）。
        /// </summary>
        public bool IsNull(string key) => !_map.TryGetValue(key, out var v) || v is JNullVal;

        public int Length() => _map.Count;

        /// <summary>鍵（插入序）。Kotlin 那側回的是 Set，這裡用 List——順序相同、用途相同。</summary>
        public List<string> Keys() => new List<string>(_order);

        // ── 轉出 ────────────────────────────────────────────────────────────
        /// <summary>緊湊 JSON（`history.jsonl` 一行一筆靠這個）。非 ASCII 不逃脫，日文原樣寫出。</summary>
        public override string ToString()
        {
            var sb = new StringBuilder();
            JsonCodec.WriteObject(this, sb);
            return sb.ToString();
        }

        internal void WriteInto(StringBuilder sb) => JsonCodec.WriteObject(this, sb);
        internal IReadOnlyList<string> OrderInternal => _order;
        internal JVal Raw(string key) => _map.TryGetValue(key, out var v) ? v : null;

        /// <summary>`map[key] as? JsonPrimitive`（JSON null 不算 primitive ⇒ 回 null）。</summary>
        private JVal Primitive(string key)
        {
            var v = Raw(key);
            return v == null || v is JNullVal ? null : v;
        }
    }

    /// <summary>可變的 JSON 陣列（對照 `org.json` 的 `JSONArray`）。</summary>
    public sealed class JArray
    {
        private readonly List<JVal> _list = new List<JVal>();

        public JArray() { }

        /// <summary>解析字串；**壞掉的 JSON 直接丟例外**。不是陣列 ⇒ 空陣列（與 Kotlin 相同）。</summary>
        public JArray(string text)
        {
            var parsed = JsonCodec.Parse(text);
            if (parsed is JArrVal a) _list.AddRange(a.Value._list);
        }

        public JArray Put(string value) { _list.Add(new JStrVal(value)); return this; }
        public JArray Put(int value) { _list.Add(JNumVal.OfInt(value)); return this; }
        public JArray Put(double value) { _list.Add(JNumVal.OfDouble(value)); return this; }
        public JArray Put(JObject value) { _list.Add(new JObjVal(value)); return this; }
        public JArray Put(JArray value) { _list.Add(new JArrVal(value)); return this; }
        public JArray PutNull() { _list.Add(JNullVal.I); return this; }

        internal JArray Add(JVal element) { _list.Add(element); return this; }

        /// <summary>
        /// 第 index 項（**物件／陣列會回自己**，與 `org.json` 的 `opt(i)` 相同）；
        /// 基本型別回 bool／int／double／string；越界、JSON null ⇒ null。
        /// </summary>
        public object Opt(int index)
        {
            if (index < 0 || index >= _list.Count) return null;
            switch (_list[index])
            {
                case JNullVal _: return null;
                case JBoolVal b: return b.Value;
                case JNumVal n:
                    var i = n.ContentIntOrNull();
                    if (i.HasValue) return i.Value;
                    var d = n.ContentDoubleOrNull();
                    if (d.HasValue) return d.Value;
                    return n.Content();
                case JStrVal s: return s.Value;
                case JObjVal o: return o.Value;
                case JArrVal a: return a.Value;
                default: return null;
            }
        }

        /// <summary>
        /// 第 index 項的內容當字串——**數字與布林會被轉成字串**（`JsonPrimitive.content` 語意），
        /// 物件／陣列 ⇒ `""`、JSON null ⇒ `"null"`（kotlinx 的 `JsonNull.content` 就是 `"null"`）、越界 ⇒ `""`。
        /// </summary>
        public string OptString(int index)
        {
            if (index < 0 || index >= _list.Count) return "";
            var v = _list[index];
            if (v is JNullVal) return "null";
            if (v is JObjVal || v is JArrVal) return "";
            return v.Content();
        }

        /// <summary>缺項／JSON null／物件／陣列 ⇒ fallback（空字串仍然是空字串）。</summary>
        public string OptString(int index, string fallback)
        {
            if (index < 0 || index >= _list.Count) return fallback;
            var v = _list[index];
            if (v is JNullVal || v is JObjVal || v is JArrVal) return fallback;
            return v.Content();
        }

        public int OptInt(int index, int fallback = 0)
        {
            if (index < 0 || index >= _list.Count) return fallback;
            var v = _list[index];
            if (v is JNullVal) return fallback;
            var i = v.ContentIntOrNull();
            if (i.HasValue) return i.Value;
            return double.TryParse(v.Content(), NumberStyles.Float, CultureInfo.InvariantCulture, out var d) ? JsonNumbers.SatInt(d) : fallback;
        }

        public JObject OptJObject(int index) => (index >= 0 && index < _list.Count && _list[index] is JObjVal o) ? o.Value : null;
        public JObject GetJSONObject(int index) => OptJObject(index) ?? throw new KeyNotFoundException("no object at " + index);
        public JArray OptJArray(int index) => (index >= 0 && index < _list.Count && _list[index] is JArrVal a) ? a.Value : null;

        public int Length() => _list.Count;

        public override string ToString()
        {
            var sb = new StringBuilder();
            JsonCodec.WriteArray(this, sb);
            return sb.ToString();
        }

        internal void WriteInto(StringBuilder sb) => JsonCodec.WriteArray(this, sb);
        internal IReadOnlyList<JVal> ItemsInternal => _list;

        /// <summary>由一串字串建（`JArray.ofStrings(...)`）。</summary>
        public static JArray OfStrings(IEnumerable<string> items)
        {
            var a = new JArray();
            foreach (var s in items) a.Put(s);
            return a;
        }

        /// <summary>由一串 [JObject] 建。</summary>
        public static JArray OfObjects(IEnumerable<JObject> items)
        {
            var a = new JArray();
            foreach (var o in items) a.Put(o);
            return a;
        }
    }

    // ── 內部值模型 ────────────────────────────────────────────────────────────

    /// <summary>
    /// 數字的**飽和轉換**——Kotlin 的 `Double.toInt()`／`toLong()` 是**飽和**的（超出範圍夾在
    /// `Int.MAX_VALUE`／`Int.MIN_VALUE`；NaN ⇒ 0），而 C# 的 `(int)d` 是**未定義**（x64 上給
    /// `int.MinValue`）⇒ 直接寫會讓「很大／很小的數字」在兩邊不同。這條是 fixture 抓出來的
    /// （`optInt` 遇上 `1e10` 這種值）。
    /// </summary>
    internal static class JsonNumbers
    {
        internal static int SatInt(double d)
        {
            if (double.IsNaN(d)) return 0;
            if (d >= int.MaxValue) return int.MaxValue;
            if (d <= int.MinValue) return int.MinValue;
            return (int)d;
        }

        internal static long SatLong(double d)
        {
            if (double.IsNaN(d)) return 0L;
            if (d >= long.MaxValue) return long.MaxValue;
            if (d <= long.MinValue) return long.MinValue;
            return (long)d;
        }
    }

    internal abstract class JVal
    {
        /// <summary>`JsonPrimitive.content`：字串回自己、數字／布林回字面表示。</summary>
        public abstract string Content();
        public virtual int? ContentIntOrNull() => null;
        public virtual long? ContentLongOrNull() => null;
        public virtual double? ContentDoubleOrNull() => null;
        public virtual bool? ContentBoolOrNull() => null;
    }

    internal sealed class JNullVal : JVal
    {
        public static readonly JNullVal I = new JNullVal();
        private JNullVal() { }
        public override string Content() => "null";
    }

    internal sealed class JBoolVal : JVal
    {
        public readonly bool Value;
        public JBoolVal(bool value) { Value = value; }
        public override string Content() => Value ? "true" : "false";
        public override bool? ContentBoolOrNull() => Value;
    }

    /// <summary>
    /// 數字：解析進來的**保留原字面值**（與 kotlinx 相同——`1.50` 原樣吐回），
    /// 程式寫入的則記整數／浮點，輸出時走 Kotlin／Java 的格式。
    /// </summary>
    internal sealed class JNumVal : JVal
    {
        private readonly long _i;
        private readonly double _d;
        private readonly bool _integral;
        private readonly string _raw;

        private JNumVal(long i, double d, bool integral, string raw)
        { _i = i; _d = d; _integral = integral; _raw = raw; }

        public static JNumVal OfInt(int value) => new JNumVal(value, value, true, null);
        public static JNumVal OfLong(long value) => new JNumVal(value, value, true, null);
        public static JNumVal OfDouble(double value) => new JNumVal((long)value, value, false, null);
        public static JNumVal Parsed(string raw, long i, double d, bool integral) => new JNumVal(i, d, integral, raw);

        public override string Content() => _raw ?? (_integral ? _i.ToString(CultureInfo.InvariantCulture) : JavaDouble.ToString(_d));

        public override int? ContentIntOrNull()
        {
            var c = Content();
            if (int.TryParse(c, NumberStyles.Integer, CultureInfo.InvariantCulture, out var i)) return i;
            return null;
        }

        public override long? ContentLongOrNull()
        {
            var c = Content();
            if (long.TryParse(c, NumberStyles.Integer, CultureInfo.InvariantCulture, out var l)) return l;
            return null;
        }

        public override double? ContentDoubleOrNull()
        {
            var c = Content();
            if (double.TryParse(c, NumberStyles.Float, CultureInfo.InvariantCulture, out var d)) return d;
            return null;
        }

        public void Write(StringBuilder sb)
        {
            if (_raw != null) { sb.Append(_raw); return; }
            if (_integral) sb.Append(_i.ToString(CultureInfo.InvariantCulture));
            else sb.Append(JavaDouble.ToString(_d));
        }
    }

    internal sealed class JStrVal : JVal
    {
        public readonly string Value;
        public JStrVal(string value) { Value = value; }
        public override string Content() => Value;
    }

    internal sealed class JObjVal : JVal
    {
        public readonly JObject Value;
        public JObjVal(JObject value) { Value = value; }
        public override string Content() => Value.ToString();
    }

    internal sealed class JArrVal : JVal
    {
        public readonly JArray Value;
        public JArrVal(JArray value) { Value = value; }
        public override string Content() => Value.ToString();
    }

    // ── 序列化／解析 ──────────────────────────────────────────────────────────

    internal static class JsonCodec
    {
        internal static void WriteObject(JObject o, StringBuilder sb)
        {
            sb.Append('{');
            var first = true;
            foreach (var k in o.OrderInternal)
            {
                if (!first) sb.Append(',');
                first = false;
                WriteString(k, sb);
                sb.Append(':');
                WriteVal(o.Raw(k), sb);
            }
            sb.Append('}');
        }

        internal static void WriteArray(JArray a, StringBuilder sb)
        {
            sb.Append('[');
            var first = true;
            foreach (var v in a.ItemsInternal)
            {
                if (!first) sb.Append(',');
                first = false;
                WriteVal(v, sb);
            }
            sb.Append(']');
        }

        private static void WriteVal(JVal v, StringBuilder sb)
        {
            switch (v)
            {
                case null:
                case JNullVal _: sb.Append("null"); break;
                case JBoolVal b: sb.Append(b.Value ? "true" : "false"); break;
                case JNumVal n: n.Write(sb); break;
                case JStrVal s: WriteString(s.Value, sb); break;
                case JObjVal o: WriteObject(o.Value, sb); break;
                case JArrVal a: WriteArray(a.Value, sb); break;
                default: sb.Append("null"); break;
            }
        }

        /// <summary>
        /// 字串逃脫——**與 kotlinx 相同**：`"` `\` `\n` `\r` `\t` `\b` `\f` 走短寫法、
        /// 其餘控制字元走 `\u00xx`（小寫十六進位）、**非 ASCII 原樣寫出**（日文、emoji 都不逃脫）。
        /// </summary>
        internal static void WriteString(string s, StringBuilder sb)
        {
            sb.Append('"');
            foreach (var c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    case '\b': sb.Append("\\b"); break;
                    case '\f': sb.Append("\\f"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                        else sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
        }

        // ── 解析 ─────────────────────────────────────────────────────────────

        internal static JVal Parse(string text)
        {
            if (text == null) throw new FormatException("null JSON text");
            var i = 0;
            SkipWs(text, ref i);
            var v = ParseVal(text, ref i);
            SkipWs(text, ref i);
            if (i != text.Length) throw new FormatException("trailing text at " + i);
            return v;
        }

        private static JVal ParseVal(string t, ref int i)
        {
            if (i >= t.Length) throw new FormatException("unexpected end");
            var c = t[i];
            switch (c)
            {
                case '{': return ParseObject(t, ref i);
                case '[': return ParseArray(t, ref i);
                case '"': return new JStrVal(ParseString(t, ref i));
                default:
                    if (c == '-' || (c >= '0' && c <= '9')) return ParseNumber(t, ref i);
                    // kotlinx 的詞法器**寬鬆**：不帶引號的單一 token 當字串（`oops` ⇒ 字串 "oops"）；
                    // 只有「多出來的內容」（`not json`）才丟。這條 fixture 鎖住了（`array_from_text` 第 8 步）。
                    var token = ReadToken(t, ref i);
                    if (token.Length == 0) throw new FormatException("unexpected char '" + c + "' at " + i);
                    switch (token)
                    {
                        case "true": return new JBoolVal(true);
                        case "false": return new JBoolVal(false);
                        case "null": return JNullVal.I;
                        default: return new JStrVal(token);
                    }
            }
        }

        /// <summary>讀一個不帶引號的 token（到分隔字元為止）。</summary>
        private static string ReadToken(string t, ref int i)
        {
            var start = i;
            while (i < t.Length && !IsDelimiter(t[i])) i++;
            return t.Substring(start, i - start);
        }

        private static bool IsDelimiter(char c) =>
            c == ' ' || c == '\t' || c == '\n' || c == '\r' ||
            c == ',' || c == '{' || c == '}' || c == '[' || c == ']' || c == ':';

        private static JVal ParseObject(string t, ref int i)
        {
            var o = new JObject();
            i++; // {
            SkipWs(t, ref i);
            if (i < t.Length && t[i] == '}') { i++; return new JObjVal(o); }
            while (true)
            {
                SkipWs(t, ref i);
                if (i >= t.Length || t[i] != '"') throw new FormatException("expected key at " + i);
                var key = ParseString(t, ref i);
                SkipWs(t, ref i);
                if (i >= t.Length || t[i] != ':') throw new FormatException("expected ':' at " + i);
                i++;
                SkipWs(t, ref i);
                var val = ParseVal(t, ref i);
                o.Set(key, val);
                SkipWs(t, ref i);
                if (i >= t.Length) throw new FormatException("unterminated object");
                if (t[i] == ',') { i++; continue; }
                if (t[i] == '}') { i++; return new JObjVal(o); }
                throw new FormatException("expected ',' or '}' at " + i);
            }
        }

        private static JVal ParseArray(string t, ref int i)
        {
            var a = new JArray();
            i++; // [
            SkipWs(t, ref i);
            if (i < t.Length && t[i] == ']') { i++; return new JArrVal(a); }
            while (true)
            {
                SkipWs(t, ref i);
                a.Add(ParseVal(t, ref i));
                SkipWs(t, ref i);
                if (i >= t.Length) throw new FormatException("unterminated array");
                if (t[i] == ',') { i++; continue; }
                if (t[i] == ']') { i++; return new JArrVal(a); }
                throw new FormatException("expected ',' or ']' at " + i);
            }
        }

        private static string ParseString(string t, ref int i)
        {
            i++; // opening quote
            var sb = new StringBuilder();
            while (true)
            {
                if (i >= t.Length) throw new FormatException("unterminated string");
                var c = t[i++];
                if (c == '"') return sb.ToString();
                if (c != '\\') { sb.Append(c); continue; }
                if (i >= t.Length) throw new FormatException("unterminated escape");
                var e = t[i++];
                switch (e)
                {
                    case '"': sb.Append('"'); break;
                    case '\\': sb.Append('\\'); break;
                    case '/': sb.Append('/'); break;
                    case 'b': sb.Append('\b'); break;
                    case 'f': sb.Append('\f'); break;
                    case 'n': sb.Append('\n'); break;
                    case 'r': sb.Append('\r'); break;
                    case 't': sb.Append('\t'); break;
                    case 'u':
                        if (i + 4 > t.Length) throw new FormatException("bad \\u escape");
                        sb.Append((char)Convert.ToInt32(t.Substring(i, 4), 16));
                        i += 4;
                        break;
                    default: throw new FormatException("bad escape '\\" + e + "'");
                }
            }
        }

        private static JVal ParseNumber(string t, ref int i)
        {
            var start = i;
            var integral = true;
            if (i < t.Length && t[i] == '-') i++;
            while (i < t.Length && t[i] >= '0' && t[i] <= '9') i++;
            if (i < t.Length && t[i] == '.')
            {
                integral = false;
                i++;
                while (i < t.Length && t[i] >= '0' && t[i] <= '9') i++;
            }
            if (i < t.Length && (t[i] == 'e' || t[i] == 'E'))
            {
                integral = false;
                i++;
                if (i < t.Length && (t[i] == '+' || t[i] == '-')) i++;
                while (i < t.Length && t[i] >= '0' && t[i] <= '9') i++;
            }
            var raw = t.Substring(start, i - start);
            if (raw.Length == 0 || raw == "-") throw new FormatException("bad number at " + start);
            long l = 0;
            var hasLong = long.TryParse(raw, NumberStyles.Integer, CultureInfo.InvariantCulture, out l);
            double d = 0;
            var hasDouble = double.TryParse(raw, NumberStyles.Float, CultureInfo.InvariantCulture, out d);
            if (!hasDouble) throw new FormatException("bad number: " + raw);
            // 整數但超出 long ⇒ 當浮點（與 kotlinx 的 `intOrNull → doubleOrNull` 順序一致）
            return JNumVal.Parsed(raw, hasLong ? l : 0L, d, integral && hasLong);
        }

        private static void SkipWs(string t, ref int i)
        {
            while (i < t.Length && (t[i] == ' ' || t[i] == '\t' || t[i] == '\n' || t[i] == '\r')) i++;
        }
    }

    /// <summary>
    /// **Java／Kotlin 的 `Double.toString()`**——C# 的預設格式不同，這支是為了逐位元相同：
    /// 整數值要帶 `.0`（`1.0`、`2520.0`）、`10^-3 ≤ |x| < 10^7` 走小數、其餘走科學記號
    /// （`1.0E21`、`1.5E-7`——指數**不補零**、尾數**至少一位小數**）。
    /// </summary>
    internal static class JavaDouble
    {
        internal static string ToString(double d)
        {
            if (double.IsNaN(d)) return "NaN";
            if (double.IsPositiveInfinity(d)) return "Infinity";
            if (double.IsNegativeInfinity(d)) return "-Infinity";
            if (d == 0.0) return double.IsNegative(d) ? "-0.0" : "0.0";

            var sign = d < 0 ? "-" : "";
            var a = Math.Abs(d);

            // 最短可回讀位數（.NET Core 3+ 的 "R" 就是最短回讀表示）
            var r = a.ToString("R", CultureInfo.InvariantCulture);
            var mantissa = r;
            var exp10 = 0;
            var eIdx = r.IndexOfAny(new[] { 'E', 'e' });
            if (eIdx >= 0)
            {
                mantissa = r.Substring(0, eIdx);
                exp10 = int.Parse(r.Substring(eIdx + 1), CultureInfo.InvariantCulture);
            }
            var dot = mantissa.IndexOf('.');
            if (dot >= 0)
            {
                exp10 -= mantissa.Length - dot - 1;
                mantissa = mantissa.Substring(0, dot) + mantissa.Substring(dot + 1);
            }
            // 去掉前導零（0.094 這種在 "R" 下會是 "0.094"）
            var lead = 0;
            while (lead < mantissa.Length - 1 && mantissa[lead] == '0') lead++;
            mantissa = mantissa.Substring(lead);
            // 去尾零（2520 → "2520" 實際上來自 2520×10^0，要變成 digits=252、exp=1）
            while (mantissa.Length > 1 && mantissa[mantissa.Length - 1] == '0')
            {
                mantissa = mantissa.Substring(0, mantissa.Length - 1);
                exp10++;
            }

            var digits = mantissa;
            var decExp = digits.Length + exp10; // m = 0.d1d2… × 10^decExp

            if (decExp > -3 && decExp <= 7)
            {
                var sb = new StringBuilder();
                if (decExp <= 0)
                {
                    sb.Append("0.");
                    for (var k = 0; k < -decExp; k++) sb.Append('0');
                    sb.Append(digits);
                }
                else if (digits.Length <= decExp)
                {
                    sb.Append(digits);
                    for (var k = 0; k < decExp - digits.Length; k++) sb.Append('0');
                    sb.Append(".0");
                }
                else
                {
                    sb.Append(digits.Substring(0, decExp));
                    sb.Append('.');
                    sb.Append(digits.Substring(decExp));
                }
                return sign + sb.ToString();
            }

            var exp = decExp - 1;
            var frac = digits.Length > 1 ? digits.Substring(1) : "0";
            return sign + digits[0] + "." + frac + "E" + exp.ToString(CultureInfo.InvariantCulture);
        }
    }
}
