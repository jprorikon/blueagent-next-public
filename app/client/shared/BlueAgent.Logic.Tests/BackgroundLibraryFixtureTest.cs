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
    /// **fixture 對拍**：`fixtures/background_library.json` 是 Kotlin 那一側（`BackgroundLibrary.kt`，
    /// 含 `BackgroundSlot`／`BackgroundRandom`）由 `BackgroundLibraryFixtureExportTest` 機器產生的真值
    /// ——內容是**資產樹（相對路徑）＋操作序列（ops）＋結果軌跡（trace）**。
    ///
    /// 這裡把同一棵資產樹舖到**記憶體 fs**（<see cref="MemFileSystem"/>）、把同一串 ops 餵進 C# 版逐條
    /// 重放，與 trace 逐字比對 ⇒ 綠＝「包規則、時段切點、隨機抽籤三件逐字相同」。
    ///
    /// ⚠️ **隨機不是比「輸出」而是比「輸入」**：`BackgroundRandom.pick` 把亂數抽成參數，fixture 記的是
    /// **餵進去的 roll**（NaN／±Inf 在 JSON 裡沒有字面值 ⇒ 寫成字串，這裡解析）。
    ///
    /// fixture 是**產物**，不要手改（要加 case 就改 Kotlin 那支匯出測試再重跑）。
    /// </summary>
    [TestFixture]
    public class BackgroundLibraryFixtureTest
    {
        /// <summary>記憶體 fs 上的相對根（與真實路徑／機器無關；兩邊都只用相對路徑）。</summary>
        private const string Root = "/assets";

        /// <summary>這條 fixture 的 case 數下限（匯出那支是不是忘了加 case）。</summary>
        private const int MinCases = 12;

        private JsonDocument _fixture;

        [SetUp]
        public void Load()
        {
            var path = Path.Combine(AppContext.BaseDirectory, "fixtures", "background_library.json");
            Assert.That(File.Exists(path), Is.True,
                $"找不到 fixture：{path}（先在 Kotlin 側跑 BackgroundLibraryFixtureExportTest）");
            _fixture = JsonDocument.Parse(File.ReadAllText(path));
        }

        [TearDown]
        public void Unload() => _fixture?.Dispose();

        /// <summary>時段清單本身也是契約（順序＝選單畫的順序）——先把它釘住。</summary>
        [Test]
        public void 時段清單與_Kotlin_版相同()
        {
            var expected = new List<string>();
            foreach (var s in _fixture.RootElement.GetProperty("slots").EnumerateArray()) expected.Add(s.GetString());
            Assert.That(BackgroundLibrary.Slots, Is.EqualTo(expected), "時段清單（順序）不同");
        }

        [Test]
        public void 逐條重放並比對軌跡()
        {
            var root = _fixture.RootElement;
            Assert.That(root.GetProperty("module").GetString(), Is.EqualTo("BackgroundLibrary"), "fixture 的 module 不對");

            var files = ReadFiles(root.GetProperty("files"));
            var cases = root.GetProperty("cases");
            var total = 0;

            foreach (var c in cases.EnumerateArray())
            {
                var name = c.GetProperty("name").GetString();
                var ops = c.GetProperty("ops");
                var trace = c.GetProperty("trace");

                Assert.That(trace.GetArrayLength(), Is.EqualTo(ops.GetArrayLength()),
                    $"[{name}] ops 與 trace 的步數不同（fixture 壞了）");

                var actual = Run(files, ops);
                var expected = new List<string>();
                foreach (var t in trace.EnumerateArray()) expected.Add(t.GetString());

                Assert.That(actual.Count, Is.EqualTo(expected.Count), $"[{name}] 步驟數不符");
                for (var i = 0; i < expected.Count; i++)
                {
                    Assert.That(actual[i], Is.EqualTo(expected[i]),
                        $"[{name}] 第 {i + 1} 步（op={OpName(ops[i])}）不符：" +
                        $"Kotlin={Brief(expected[i])} / C#={Brief(actual[i])}");
                }
                total += expected.Count;
            }

            Assert.That(cases.GetArrayLength(), Is.GreaterThanOrEqualTo(MinCases), "fixture 太少（匯出那支是不是忘了加 case）");
            TestContext.Out.WriteLine(
                $"background_library.json：{cases.GetArrayLength()} 條 case／{total} 步與 Kotlin 版逐字相同");
        }

        /// <summary>
        /// 守的是**fixture 本身的可移植性**（不是模組行為）：`files` 與 ops 裡不能有絕對路徑。
        /// </summary>
        [Test]
        public void fixture_不含絕對路徑()
        {
            var root = _fixture.RootElement;
            foreach (var f in root.GetProperty("files").EnumerateArray())
            {
                var p = f.GetProperty("path").GetString();
                Assert.That(p.StartsWith("/", StringComparison.Ordinal), Is.False, $"files 的 path 是絕對路徑：{Brief(p)}");
            }
            var cases = root.GetProperty("cases");
            foreach (var c in cases.EnumerateArray())
            {
                var name = c.GetProperty("name").GetString();
                foreach (var op in c.GetProperty("ops").EnumerateArray())
                {
                    foreach (var prop in op.EnumerateObject())
                    {
                        if (prop.Value.ValueKind != JsonValueKind.String) continue;
                        var v = prop.Value.GetString();
                        if (string.IsNullOrEmpty(v)) continue;
                        if (prop.Name == "dir" || prop.Name == "root")
                        {
                            Assert.Fail($"[{name}] op 帶了路徑欄位 {prop.Name}（fixture 不得含絕對路徑）");
                        }
                        // `roll` 是 "0.5" 這種字串、`pack` 是整份 manifest 的 JSON 原文——兩者都不會是路徑。
                        Assert.That(v.StartsWith("/", StringComparison.Ordinal), Is.False,
                            $"[{name}] op 的 {prop.Name} 看起來是絕對路徑：{Brief(v)}");
                    }
                }
                Assert.That(c.GetProperty("ops").GetArrayLength(), Is.GreaterThan(0), $"[{name}] 沒有 ops");
            }
            TestContext.Out.WriteLine($"BackgroundLibrary fixture 健檢：{cases.GetArrayLength()} 條 case 都不含絕對路徑");
        }

        // ── 重放器 ────────────────────────────────────────────────────────────────

        private static string OpName(JsonElement op) =>
            op.TryGetProperty("op", out var v) ? v.GetString() : "?";

        private static string Brief(string s) =>
            s == null ? "null" : (s.Length <= 400 ? s : s.Substring(0, 400) + "…");

        private static List<KeyValuePair<string, string>> ReadFiles(JsonElement files)
        {
            var outp = new List<KeyValuePair<string, string>>();
            foreach (var f in files.EnumerateArray())
            {
                outp.Add(new KeyValuePair<string, string>(
                    f.GetProperty("path").GetString(), f.GetProperty("text").GetString()));
            }
            return outp;
        }

        /// <summary>
        /// 重放（與 Kotlin 那支 `run()` 逐條對應）：每個 case 一棵新的記憶體 fs。任何一步丟例外 ⇒ `!THROW`。
        /// </summary>
        private static List<string> Run(List<KeyValuePair<string, string>> files, JsonElement ops)
        {
            var fs = new MemFileSystem();
            foreach (var f in files) fs.WriteText(Paths.Join(Root, f.Key), f.Value);
            var lib = new BackgroundLibrary(new DirAssetSource(Root, fs));

            var trace = new List<string>();
            foreach (var op in ops.EnumerateArray())
            {
                string r;
                try { r = Step(lib, op); }
                catch { r = "!THROW"; }
                trace.Add(r);
            }
            return trace;
        }

        private static string Step(BackgroundLibrary lib, JsonElement o)
        {
            var kind = o.GetProperty("op").GetString();
            switch (kind)
            {
                case "load":
                    return JArray.OfObjects(lib.Load()).ToString();

                case "loadJson":
                    return lib.LoadJson();

                case "parse":
                    var has = new HashSet<string>(StringComparer.Ordinal);
                    foreach (var n in o.GetProperty("exists").EnumerateArray()) has.Add(n.GetString());
                    var pack = BackgroundLibrary.Parse(
                        o.GetProperty("id").GetString(),
                        o.GetProperty("pack").GetString(),
                        name => has.Contains(name));
                    return pack == null ? "null" : pack.ToString();

                case "forHour":
                    return BackgroundSlot.ForHour(o.GetProperty("hour").GetInt32());

                case "pick":
                    var ids = new List<string>();
                    foreach (var v in o.GetProperty("ids").EnumerateArray()) ids.Add(v.GetString());
                    return BackgroundRandom.Pick(
                        ids,
                        o.GetProperty("current").GetString(),
                        ParseRoll(o.GetProperty("roll").GetString()));

                default:
                    return "!UNKNOWN_OP:" + kind;
            }
        }

        /// <summary>
        /// `roll` 一律是**字串**（NaN／±Inf 在 JSON 裡沒有字面值）——與 Kotlin 那支匯出器的 `parseRoll` 同一條規則。
        /// </summary>
        private static double ParseRoll(string s)
        {
            switch (s)
            {
                case "NaN": return double.NaN;
                case "Infinity": return double.PositiveInfinity;
                case "-Infinity": return double.NegativeInfinity;
                default: return double.Parse(s, CultureInfo.InvariantCulture);
            }
        }
    }
}
