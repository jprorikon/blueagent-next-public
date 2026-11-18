using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading;
using BlueAgent.Logic;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// **平台縫（Foundation）的 fixture 對拍**：`fixtures/foundation.json` 是 Kotlin 那一側
    /// （`commonMain/Fs.kt`）由 `FoundationFixtureExportTest` 機器產生的真值。這裡把同一批輸入／
    /// 同一串 ops 餵進 C# 版（`Foundation.cs`＋`MemFileSystem.cs`），與 Kotlin 的輸出逐條比對。
    ///
    /// 三塊：
    ///  * `pathJoin` —— 逐條（輸入片段 ⇒ 路徑字串）。
    ///  * `MapSettingsBackend` —— **ops＋trace**（有狀態門面：只比對總和會漏掉順序）。
    ///  * `DirAssetSource` —— fixture 只有相對路徑 ⇒ 用 <see cref="MemFileSystem"/> 在
    ///    <see cref="MemRoot"/> 底下重建同一棵目錄再逐條比對（記憶體 fs 的 list 語意已對齊）。
    ///
    /// 另外兩支**不靠 fixture 的機器斷言**：<see cref="RecursiveLock"/> 的可重入（Guard 裡再 Guard
    /// 不死鎖）與 <see cref="MemFileSystem"/> 的讀寫／追加／列表／搬移語義。
    ///
    /// ⚠️ 不一致時**不是**讓第一個 Assert 直接炸掉，而是把該塊的**所有**差異收成一張清單再一次報出來
    /// ——這樣一次跑完就能看到「哪幾條、Kotlin 給什麼、C# 給什麼」。（fixture 是真值：修的是
    /// `Foundation.cs`，不是這支測試。）
    ///
    /// fixture 是**產物**，不要手改。
    /// </summary>
    [TestFixture]
    public class FoundationFixtureTest
    {
        /// <summary>在記憶體檔案系統上挑一個「絕對」根；fixture 內只有相對路徑 ⇒ 這個字串不需要跟 Kotlin 一樣。</summary>
        private const string MemRoot = "/assets";

        private JsonDocument _fixture;

        [SetUp]
        public void Load()
        {
            var path = Path.Combine(AppContext.BaseDirectory, "fixtures", "foundation.json");
            Assert.That(File.Exists(path), Is.True,
                $"找不到 fixture：{path}（先在 Kotlin 側跑 FoundationFixtureExportTest）");
            _fixture = JsonDocument.Parse(File.ReadAllText(path));
        }

        [TearDown]
        public void Unload() => _fixture?.Dispose();

        // ── pathJoin ────────────────────────────────────────────────────────────

        [Test]
        public void pathJoin_與_Kotlin_版逐條相同()
        {
            var cases = _fixture.RootElement.GetProperty("pathJoin").GetProperty("cases");
            var diffs = new List<string>();
            var count = 0;

            foreach (var c in cases.EnumerateArray())
            {
                var name = c.GetProperty("name").GetString();
                var parts = Strings(c.GetProperty("parts"));
                var expected = c.GetProperty("out").GetString();
                var actual = Paths.Join(parts.ToArray());

                count++;
                if (actual != expected)
                    diffs.Add($"[{name}] parts={Fmt(parts)} Kotlin={Q(expected)} / C#={Q(actual)}");
            }

            Assert.That(count, Is.GreaterThan(15), "fixture 太少（匯出那支是不是忘了加 case）");
            Assert.That(diffs, Is.Empty, "pathJoin 與 Kotlin 版不同：\n" + string.Join("\n", diffs));
            TestContext.Out.WriteLine($"pathJoin：{count} 條與 Kotlin 版相同");
        }

        // ── MapSettingsBackend（ops ＋ trace）────────────────────────────────────

        [Test]
        public void MapSettingsBackend_逐條軌跡與_Kotlin_版相同()
        {
            var cases = _fixture.RootElement.GetProperty("settings").GetProperty("cases");
            var diffs = new List<string>();
            var count = 0;
            var steps = 0;

            foreach (var c in cases.EnumerateArray())
            {
                var name = c.GetProperty("name").GetString();
                var ops = c.GetProperty("ops");
                var expected = Strings(c.GetProperty("trace"));
                var actual = RunSettings(ops);

                count++;
                if (actual.Count != expected.Count)
                {
                    diffs.Add($"[{name}] 步數不符：Kotlin={expected.Count} / C#={actual.Count}");
                    continue;
                }

                for (var i = 0; i < expected.Count; i++)
                {
                    steps++;
                    if (actual[i] != expected[i])
                        diffs.Add($"[{name}] 第 {i + 1} 步（op={OpName(ops[i])}）" +
                                  $"Kotlin={Q(expected[i])} / C#={Q(actual[i])}");
                }
            }

            Assert.That(count, Is.GreaterThan(3), "fixture 太少（匯出那支是不是忘了加 case）");
            Assert.That(diffs, Is.Empty,
                "MapSettingsBackend 與 Kotlin 版不同（`getInt`／`getFloat` 的型別判定與轉換是常見走鐘點）：\n"
                + string.Join("\n", diffs));
            TestContext.Out.WriteLine($"MapSettingsBackend：{count} 條 case／{steps} 步與 Kotlin 版相同");
        }

        /// <summary>重放（與 Kotlin 那支 `runSettings()` 逐條對應）。</summary>
        private static List<string> RunSettings(JsonElement ops)
        {
            MapSettingsBackend backend = null;
            var trace = new List<string>();
            foreach (var o in ops.EnumerateArray())
            {
                string r;
                try { r = SettingsStep(ref backend, o); }
                catch { r = "!THROW"; }
                trace.Add(r);
            }
            return trace;
        }

        private static string SettingsStep(ref MapSettingsBackend b, JsonElement o)
        {
            var kind = o.GetProperty("op").GetString();
            switch (kind)
            {
                case "new": b = new MapSettingsBackend(); return "ok";
                case "new_seed": b = new MapSettingsBackend(Seed(o.GetProperty("seed"))); return "ok";

                case "getString": return b.GetString(Key(o), o.GetProperty("def").GetString());
                case "getInt":
                    return b.GetInt(Key(o), o.GetProperty("def").GetInt32()).ToString(CultureInfo.InvariantCulture);
                case "getBoolean": return b.GetBoolean(Key(o), o.GetProperty("def").GetBoolean()) ? "true" : "false";
                case "getFloat": return NumF(b.GetFloat(Key(o), (float)o.GetProperty("def").GetDouble()));

                case "putString": b.PutString(Key(o), o.GetProperty("value").GetString()); return "ok";
                case "putInt": b.PutInt(Key(o), o.GetProperty("value").GetInt32()); return "ok";
                case "putBoolean": b.PutBoolean(Key(o), o.GetProperty("value").GetBoolean()); return "ok";
                case "putFloat": b.PutFloat(Key(o), (float)o.GetProperty("value").GetDouble()); return "ok";

                case "remove": b.Remove(Key(o)); return "ok";
                case "contains": return b.Contains(Key(o)) ? "true" : "false";
                case "keys": return string.Join(",", b.Keys());
                default: return "!UNKNOWN_OP:" + kind;
            }
        }

        /// <summary>fixture 的 seed 條目 ⇒ 種子（型別照原樣：int／long／double／bool／string）。</summary>
        private static IEnumerable<KeyValuePair<string, object>> Seed(JsonElement seed)
        {
            var list = new List<KeyValuePair<string, object>>();
            foreach (var e in seed.EnumerateArray())
            {
                var key = e.GetProperty("key").GetString();
                var value = e.GetProperty("value");
                var type = e.GetProperty("type").GetString();
                object v;
                switch (type)
                {
                    case "str": v = value.GetString(); break;
                    case "int": v = value.GetInt32(); break;
                    case "long": v = value.GetInt64(); break;
                    case "double": v = value.GetDouble(); break;
                    case "bool": v = value.GetBoolean(); break;
                    default: throw new ArgumentException("bad seed type: " + type);
                }
                list.Add(new KeyValuePair<string, object>(key, v));
            }
            return list;
        }

        // ── DirAssetSource（用 MemFileSystem 重建）───────────────────────────────

        [Test]
        public void DirAssetSource_用_MemFileSystem_重建後逐條相同()
        {
            var dirAsset = _fixture.RootElement.GetProperty("dirAsset");
            var files = dirAsset.GetProperty("files");

            var fs = new MemFileSystem();
            var fileCount = 0;
            foreach (var f in files.EnumerateArray())
            {
                var rel = f.GetProperty("path").GetString();
                var text = f.GetProperty("text").GetString();
                Assert.That(fs.WriteText(Paths.Join(MemRoot, rel), text), Is.True, $"寫不進記憶體 fs：{rel}");
                fileCount++;
            }

            var assets = new DirAssetSource(MemRoot, fs);
            var diffs = new List<string>();
            var rows = 0;

            foreach (var c in dirAsset.GetProperty("listCases").EnumerateArray())
            {
                var name = c.GetProperty("name").GetString();
                var d = c.GetProperty("dir").GetString();
                var expected = Strings(c.GetProperty("out"));
                var actual = assets.List(d);
                // `File.list()` 的順序未定義 ⇒ **兩邊都**排序後比（只排一邊等於把未定義的順序當契約）。
                expected.Sort();
                actual.Sort();

                rows++;
                if (!actual.SequenceEqual(expected))
                    diffs.Add($"[list/{name}] dir={Q(d)} Kotlin={Fmt(expected)} / C#={Fmt(actual)}");
            }

            foreach (var c in dirAsset.GetProperty("readTextCases").EnumerateArray())
            {
                var name = c.GetProperty("name").GetString();
                var rel = c.GetProperty("path").GetString();
                var outEl = c.GetProperty("out");
                var expected = outEl.ValueKind == JsonValueKind.Null ? null : outEl.GetString();
                var actual = assets.ReadText(rel);

                rows++;
                if (actual != expected)
                    diffs.Add($"[readText/{name}] path={Q(rel)} Kotlin={Q(expected)} / C#={Q(actual)}");
            }

            foreach (var c in dirAsset.GetProperty("existsCases").EnumerateArray())
            {
                var name = c.GetProperty("name").GetString();
                var rel = c.GetProperty("path").GetString();
                var expected = c.GetProperty("out").GetBoolean();
                var actual = assets.Exists(rel);

                rows++;
                if (actual != expected)
                    diffs.Add($"[exists/{name}] path={Q(rel)} Kotlin={expected} / C#={actual}");
            }

            Assert.That(fileCount, Is.GreaterThan(3), "fixture 沒給檔案清單");
            Assert.That(rows, Is.GreaterThan(10), "fixture 的 asset case 太少");
            Assert.That(diffs, Is.Empty,
                $"DirAssetSource 與 Kotlin 版不同（fixture 的根＝{MemRoot}）：\n" + string.Join("\n", diffs));
            TestContext.Out.WriteLine($"DirAssetSource：{rows} 條（{fileCount} 檔）與 Kotlin 版相同");
        }

        // ── 不靠 fixture 的機器斷言 ──────────────────────────────────────────────

        /// <summary>
        /// `RecursiveLock` 的契約有兩半：**可重入**（Kotlin 是 `ReentrantLock`／`NSRecursiveLock`——
        /// 寫入路徑裡會再進鎖，例如 append 內部叫 rewrite）與**互斥**。這裡兩半都機器驗。
        /// </summary>
        [Test]
        public void RecursiveLock_可重入且互斥()
        {
            var lk = new RecursiveLock();

            // ① 有回傳值的 Guard：裡面再 Guard 兩層——不可重入就會在這裡自己鎖死。
            var depth = 0;
            var value = lk.Guard(() =>
            {
                depth++;
                return lk.Guard(() =>
                {
                    depth++;
                    return lk.Guard(() =>
                    {
                        depth++;            // ⚠️ 內層也要 ++：不然回傳的是「還沒 ++ 的 depth」＝2
                        return depth;       //    （原本那條測試錯在把「讀值」當成「再進一層」）
                    });
                });
            });
            Assert.That(value, Is.EqualTo(3), "三層 Guard 的回傳值不對（可重入？）");
            Assert.That(depth, Is.EqualTo(3));

            // ② 純動作的 Guard 也要放得掉鎖（不然下面的執行緒會卡住）。
            var ran = false;
            lk.Guard(() => { ran = true; });
            Assert.That(ran, Is.True);
            Assert.That(lk.Guard(() => 5), Is.EqualTo(5), "Guard 之後鎖沒放掉／回傳值是錯的");

            // ③ 互斥：兩個執行緒對著同一把鎖做「讀-改-寫」，沒有互斥就會掉計數。
            var shared = 0;
            var t1 = new Thread(() => { for (var i = 0; i < 20000; i++) lk.Guard(() => { var v = shared; shared = v + 1; }); });
            var t2 = new Thread(() => { for (var i = 0; i < 20000; i++) lk.Guard(() => { var v = shared; shared = v + 1; }); });
            t1.Start();
            t2.Start();
            Assert.That(t1.Join(10000), Is.True, "t1 卡住（死鎖？）");
            Assert.That(t2.Join(10000), Is.True, "t2 卡住（死鎖？）");
            Assert.That(shared, Is.EqualTo(40000), "Guard 沒有互斥（計數掉了）");

            TestContext.Out.WriteLine($"RecursiveLock：可重入（3 層）＋ 2×20000 次互斥計數 = {shared}");
        }

        /// <summary>`MemFileSystem` 的語義（讀不到 ⇒ null、寫入自動建上層、List 只回該層檔名…）。</summary>
        [Test]
        public void MemFileSystem_讀寫追加列表搬移語義()
        {
            var fs = new MemFileSystem();

            Assert.That(fs.ReadText("/d/a.txt"), Is.Null, "讀不到要回 null（不丟例外）");
            Assert.That(fs.Exists("/d/a.txt"), Is.False);
            Assert.That(fs.ReadBytes("/d/a.txt"), Is.Null);

            Assert.That(fs.WriteText("/d/a.txt", "一"), Is.True, "覆寫寫入要自動建上層目錄");
            Assert.That(fs.ReadText("/d/a.txt"), Is.EqualTo("一"));
            Assert.That(fs.Exists("/d/a.txt"), Is.True);
            Assert.That(fs.Count, Is.EqualTo(1));

            Assert.That(fs.AppendText("/d/a.txt", "二"), Is.True);
            Assert.That(fs.ReadText("/d/a.txt"), Is.EqualTo("一二"), "追加要接在後面");
            Assert.That(fs.AppendText("/d/none.txt", "新"), Is.True, "追加到不存在的檔要自己開一個");
            Assert.That(fs.ReadText("/d/none.txt"), Is.EqualTo("新"));

            Assert.That(fs.WriteText("/d/a.txt", "覆蓋"), Is.True);
            Assert.That(fs.ReadText("/d/a.txt"), Is.EqualTo("覆蓋"), "寫入是覆寫不是追加");

            Assert.That(fs.WriteBytes("/d/b.bin", new byte[] { 1, 2, 3 }), Is.True);
            var bytes = fs.ReadBytes("/d/b.bin");
            Assert.That(bytes, Is.EqualTo(new byte[] { 1, 2, 3 }));
            bytes[0] = 9;                                            // 動到拿到的副本
            Assert.That(fs.ReadBytes("/d/b.bin"), Is.EqualTo(new byte[] { 1, 2, 3 }), "ReadBytes 要回副本");

            // List：只回**該層**的名字、不遞迴；**子目錄名也會出現**（Kotlin 的 `File.list()` 檔案與目錄
            // 一起回，`CharacterLibrary` 那類「列出角色包目錄」的模組就靠這條）。
            fs.WriteText("/d/x.txt", "x");
            fs.WriteText("/d/sub/y.txt", "y");
            fs.WriteText("/d/sub/deep/z.txt", "z");
            Assert.That(fs.List("/d"), Is.EquivalentTo(new[] { "a.txt", "none.txt", "b.bin", "x.txt", "sub" }),
                "List 只回該層的名字（含子目錄名）");
            Assert.That(fs.List("/d/sub"), Is.EquivalentTo(new[] { "y.txt", "deep" }), "List 不遞迴");
            Assert.That(fs.List("/nope"), Is.Empty, "不存在的目錄 ⇒ 空清單");
            Assert.That(fs.List("/d/sub/deep"), Is.EquivalentTo(new[] { "z.txt" }));

            // 搬移／改名（原子上稿那條路：先寫 .tmp 再搬）。
            Assert.That(fs.Rename("/d/x.txt.tmp", "/d/x.txt"), Is.False, "來源不存在 ⇒ false");
            Assert.That(fs.Rename("/d/x.txt", "/d/x2.txt"), Is.True);
            Assert.That(fs.Exists("/d/x.txt"), Is.False, "搬移後來源不該還在");
            Assert.That(fs.ReadText("/d/x2.txt"), Is.EqualTo("x"), "搬移要帶著內容");
            Assert.That(fs.Delete("/d/x2.txt"), Is.True);
            Assert.That(fs.Delete("/d/x2.txt"), Is.False, "再刪一次 ⇒ false");
            // 剩下的：a.txt／none.txt／b.bin／sub/y.txt／sub/deep/z.txt
            Assert.That(fs.Count, Is.EqualTo(5));

            TestContext.Out.WriteLine($"MemFileSystem：讀寫／追加／列表／搬移語義全數符合（{fs.Count} 個檔）");
        }

        /// <summary>fixture 條數總表（末行報數）。</summary>
        [Test]
        public void fixture_條數()
        {
            var root = _fixture.RootElement;
            var pathJoin = root.GetProperty("pathJoin").GetProperty("cases").GetArrayLength();

            var settingsCases = root.GetProperty("settings").GetProperty("cases");
            var steps = 0;
            foreach (var c in settingsCases.EnumerateArray())
                steps += c.GetProperty("trace").GetArrayLength();

            var dirAsset = root.GetProperty("dirAsset");
            var listRows = dirAsset.GetProperty("listCases").GetArrayLength();
            var readRows = dirAsset.GetProperty("readTextCases").GetArrayLength();
            var existsRows = dirAsset.GetProperty("existsCases").GetArrayLength();
            var files = dirAsset.GetProperty("files").GetArrayLength();

            TestContext.Out.WriteLine(
                $"foundation.json：pathJoin {pathJoin} 條／MapSettingsBackend {settingsCases.GetArrayLength()} 條 case（{steps} 步）" +
                $"／DirAssetSource {listRows + readRows + existsRows} 條（list {listRows}・readText {readRows}・exists {existsRows}，{files} 檔）" +
                $" ⇒ 共 {pathJoin + settingsCases.GetArrayLength() + listRows + readRows + existsRows} 條");
        }

        // ── 小工具 ──────────────────────────────────────────────────────────────

        private static string Key(JsonElement o) => o.GetProperty("key").GetString();

        private static string OpName(JsonElement op) =>
            op.TryGetProperty("op", out var v) ? v.GetString() : "?";

        private static List<string> Strings(JsonElement array)
        {
            var list = new List<string>();
            foreach (var it in array.EnumerateArray()) list.Add(it.GetString());
            return list;
        }

        private static string Q(string s) => s == null ? "<null>" : "\"" + s + "\"";

        private static string Fmt(IEnumerable<string> xs) => "[" + string.Join(",", xs) + "]";

        /// <summary>Kotlin 的 `Float.toString()`（整數值帶 `.0`、不使用科學記號）——只有 trace 用的值。</summary>
        private static string NumF(float f)
        {
            var r = f.ToString("R", CultureInfo.InvariantCulture);
            if (r.IndexOf('.') < 0 && r.IndexOf('E') < 0 && r.IndexOf('e') < 0 &&
                !float.IsNaN(f) && !float.IsInfinity(f)) r += ".0";
            return r;
        }
    }
}
