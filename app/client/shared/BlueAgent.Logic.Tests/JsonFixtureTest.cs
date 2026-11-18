using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text.Json;
using BlueAgent.Logic;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// **fixture 對拍**：`fixtures/json.json` 是 Kotlin 那一側（`Json.kt`）由
    /// `JsonFixtureExportTest` 機器產生的真值——內容是**操作序列（ops）＋結果軌跡（trace）**。
    ///
    /// 這裡把同一串 ops 餵進 C# 版逐條重放，與 trace 逐條比對 ⇒ 綠＝「兩邊的寬鬆讀取語意與
    /// 序列化格式逐位元相同」。壞掉的 JSON 兩邊都記 `!THROW`（只比「會不會丟」，不比例外型別——
    /// 那本來就會因語言而異）。
    ///
    /// fixture 是**產物**，不要手改。
    /// </summary>
    [TestFixture]
    public class JsonFixtureTest
    {
        private JsonDocument _fixture;

        [SetUp]
        public void Load()
        {
            var path = Path.Combine(AppContext.BaseDirectory, "fixtures", "json.json");
            Assert.That(File.Exists(path), Is.True, $"找不到 fixture：{path}（先在 Kotlin 側跑匯出）");
            _fixture = JsonDocument.Parse(File.ReadAllText(path));
        }

        [TearDown]
        public void Unload() => _fixture?.Dispose();

        [Test]
        public void 逐條重放並比對軌跡()
        {
            var cases = _fixture.RootElement.GetProperty("cases");
            var total = 0;
            foreach (var c in cases.EnumerateArray())
            {
                var name = c.GetProperty("name").GetString();
                var ops = c.GetProperty("ops");
                var trace = c.GetProperty("trace");

                var actual = Run(ops);
                var expected = new List<string>();
                foreach (var t in trace.EnumerateArray()) expected.Add(t.GetString());

                Assert.That(actual.Count, Is.EqualTo(expected.Count), $"[{name}] 步驟數不符");
                for (var i = 0; i < expected.Count; i++)
                {
                    Assert.That(actual[i], Is.EqualTo(expected[i]),
                        $"[{name}] 第 {i + 1} 步（op={OpName(ops[i])}）不符：Kotlin={expected[i]} / C#={actual[i]}");
                }
                total += expected.Count;
            }
            TestContext.Out.WriteLine($"json.json：{cases.GetArrayLength()} 條 case／{total} 步全數相同");
        }

        private static string OpName(JsonElement op) =>
            op.TryGetProperty("op", out var v) ? v.GetString() : "?";

        /// <summary>重放（與 Kotlin 那支 `run()` 逐條對應）。</summary>
        private static List<string> Run(JsonElement ops)
        {
            JObject obj = null;
            JArray arr = null;
            var trace = new List<string>();
            foreach (var op in ops.EnumerateArray())
            {
                string r;
                try { r = Step(ref obj, ref arr, op); }
                catch { r = "!THROW"; }
                trace.Add(r);
            }
            return trace;
        }

        private static string Step(ref JObject obj, ref JArray arr, JsonElement o)
        {
            var kind = o.GetProperty("op").GetString();
            switch (kind)
            {
                case "obj": obj = new JObject(); return "ok";
                case "obj_text": obj = new JObject(o.GetProperty("text").GetString()); return "ok";
                case "put": PutInto(obj, o); return "ok";
                case "remove": obj.Remove(o.GetProperty("key").GetString()); return "ok";

                case "str": return obj.ToString();
                case "length": return obj.Length().ToString(CultureInfo.InvariantCulture);
                case "keys": return string.Join(",", obj.Keys());
                case "has": return obj.Has(o.GetProperty("key").GetString()) ? "true" : "false";
                case "isnull": return obj.IsNull(o.GetProperty("key").GetString()) ? "true" : "false";
                case "opt": return OptOf(obj, o);
                case "get": return GetOf(obj, o);

                case "arr": arr = new JArray(); return "ok";
                case "arr_text": arr = new JArray(o.GetProperty("text").GetString()); return "ok";
                case "push": PushInto(arr, o); return "ok";
                case "ofstrings": arr = JArray.OfStrings(Strings(o.GetProperty("items"))); return "ok";

                case "arr_str": return arr.ToString();
                case "arr_len": return arr.Length().ToString(CultureInfo.InvariantCulture);
                case "arr_opt": return AnyToString(arr.Opt(Index(o)));
                case "arr_optstr": return o.TryGetProperty("fallback", out var f1)
                    ? arr.OptString(Index(o), f1.GetString()) : arr.OptString(Index(o));
                case "arr_optint": return o.TryGetProperty("fallback", out var f2)
                    ? arr.OptInt(Index(o), f2.GetInt32()).ToString(CultureInfo.InvariantCulture)
                    : arr.OptInt(Index(o)).ToString(CultureInfo.InvariantCulture);
                case "arr_optobj": return ObjOrNull(arr.OptJObject(Index(o)));
                case "arr_getobj": return ObjOrNull(arr.GetJSONObject(Index(o)));
                case "arr_optarr":
                    var a = arr.OptJArray(Index(o));
                    return a == null ? "null" : a.ToString();
                default: return "!UNKNOWN_OP:" + kind;
            }
        }

        private static int Index(JsonElement o) => o.GetProperty("index").GetInt32();

        private static IEnumerable<string> Strings(JsonElement array)
        {
            foreach (var it in array.EnumerateArray()) yield return it.GetString();
        }

        private static void PutInto(JObject obj, JsonElement o)
        {
            var key = o.GetProperty("key").GetString();
            var type = o.GetProperty("type").GetString();
            switch (type)
            {
                case "str":
                    obj.Put(key, o.TryGetProperty("value", out var vs) && vs.ValueKind != JsonValueKind.Null ? vs.GetString() : null);
                    break;
                case "int": obj.Put(key, o.GetProperty("value").GetInt32()); break;
                case "long": obj.Put(key, o.GetProperty("value").GetInt64()); break;
                case "double": obj.Put(key, o.GetProperty("value").GetDouble()); break;
                case "bool": obj.Put(key, o.GetProperty("value").GetBoolean()); break;
                case "jnull": obj.PutNull(key); break;
                case "obj": obj.Put(key, new JObject(o.GetProperty("text").GetString())); break;
                case "arr": obj.Put(key, new JArray(o.GetProperty("text").GetString())); break;
                default: throw new ArgumentException("bad type");
            }
        }

        private static void PushInto(JArray arr, JsonElement o)
        {
            switch (o.GetProperty("type").GetString())
            {
                case "str": arr.Put(o.GetProperty("value").GetString()); break;
                case "int": arr.Put(o.GetProperty("value").GetInt32()); break;
                case "double": arr.Put(o.GetProperty("value").GetDouble()); break;
                case "obj": arr.Put(new JObject(o.GetProperty("text").GetString())); break;
                case "arr": arr.Put(new JArray(o.GetProperty("text").GetString())); break;
                case "jnull": arr.PutNull(); break;
                default: throw new ArgumentException("bad type");
            }
        }

        private static string OptOf(JObject obj, JsonElement o)
        {
            var key = o.GetProperty("key").GetString();
            var fb = o.TryGetProperty("fallback", out var f) ? f : (JsonElement?)null;
            switch (o.GetProperty("kind").GetString())
            {
                case "str": return obj.OptString(key);
                case "strfb": return obj.OptString(key, fb.Value.GetString());
                case "int": return obj.OptInt(key).ToString(CultureInfo.InvariantCulture);
                case "intfb": return obj.OptInt(key, fb.Value.GetInt32()).ToString(CultureInfo.InvariantCulture);
                case "double": return Num(obj.OptDouble(key));
                case "doublefb": return Num(obj.OptDouble(key, fb.Value.GetDouble()));
                case "long": return obj.OptLong(key, fb.Value.GetInt64()).ToString(CultureInfo.InvariantCulture);
                case "bool": return obj.OptBoolean(key) ? "true" : "false";
                case "boolfb": return obj.OptBoolean(key, fb.Value.GetBoolean()) ? "true" : "false";
                case "obj": return ObjOrNull(obj.OptJObject(key));
                case "arr":
                    var a = obj.OptJArray(key);
                    return a == null ? "null" : a.ToString();
                default: return "!UNKNOWN_KIND";
            }
        }

        private static string GetOf(JObject obj, JsonElement o)
        {
            var key = o.GetProperty("key").GetString();
            switch (o.GetProperty("kind").GetString())
            {
                case "str": return obj.GetString(key);
                case "int": return obj.GetInt(key).ToString(CultureInfo.InvariantCulture);
                case "long": return obj.GetLong(key).ToString(CultureInfo.InvariantCulture);
                case "double": return Num(obj.GetDouble(key));
                case "bool": return obj.GetBoolean(key) ? "true" : "false";
                case "obj": return obj.GetJSONObject(key).ToString();
                case "arr": return obj.GetJSONArray(key).ToString();
                default: return "!UNKNOWN_KIND";
            }
        }

        private static string ObjOrNull(JObject o) => o == null ? "null" : o.ToString();

        /// <summary>`JArray.opt(i)` 的 `Any?` ⇒ 字串（物件／陣列回自己的 JSON）。</summary>
        private static string AnyToString(object v)
        {
            switch (v)
            {
                case null: return "null";
                case bool b: return b ? "true" : "false";
                case int i: return i.ToString(CultureInfo.InvariantCulture);
                case long l: return l.ToString(CultureInfo.InvariantCulture);
                case double d: return Num(d);
                case JObject o: return o.ToString();
                case JArray a: return a.ToString();
                default: return v.ToString();
            }
        }

        /// <summary>
        /// Kotlin 的 `Double.toString()`——**借庫自己的格式化**（`JObject` 的數字本來就走 `JavaDouble`），
        /// 不要在測試裡另寫一份：原本自己排版，`1e10` 給出 `10000000000.0` 而 Kotlin 給 `1.0E10`
        /// （10^7 以上要走科學記號），害得 `number_saturation` 那條紅。
        /// </summary>
        private static string Num(double d) => new JObject().Put("x", d).OptString("x");
    }
}
