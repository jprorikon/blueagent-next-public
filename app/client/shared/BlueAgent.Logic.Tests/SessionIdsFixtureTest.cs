using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.RegularExpressions;
using BlueAgent.Logic;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// **fixture 對拍**：`fixtures/session_ids.json` 與 `fixtures/session_ids_reroll.json` 是 Kotlin 那一側
    /// （`SessionIds.kt`）由 `SessionIdsFixtureExportTest` 機器產生的真值；這裡把同一批輸入餵進 C# 版，逐欄比對。
    ///
    /// session id 是**亂數**的產物，所以真值不能只記「輸入文字 ⇒ 輸出文字」：每個 case 帶一串 `script`
    /// （依呼叫序被消耗的亂數值，Kotlin 側用一支腳本化 Random 交值）。C# 端照同一串腳本重放 ⇒ 應該得到
    /// **同一個 UUID**。這樣對到的就不只是格式，而是**消耗順序與十六進位對應**（腳本被吃掉幾次、
    /// 順序錯了，UUID 就不一樣）。
    ///
    /// ⇒ 這一支綠＝「C# 版與 Kotlin 版在這批 case 上行為相同」。fixture 是**產物**，不要手改
    /// （要加 case 就改 Kotlin 那支匯出測試再重跑）。
    /// </summary>
    [TestFixture]
    public class SessionIdsFixtureTest
    {
        /// <summary>照 Kotlin 單測／匯出那支的形狀：8-4-4-4-12、版本位 4、variant 在 89ab。</summary>
        private static readonly Regex Uuid4 =
            new Regex("^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", RegexOptions.Compiled);

        private JsonDocument _ids;
        private JsonDocument _reroll;

        [SetUp]
        public void Load()
        {
            _ids = Read("session_ids.json");
            _reroll = Read("session_ids_reroll.json");
        }

        [TearDown]
        public void Unload()
        {
            _ids?.Dispose();
            _reroll?.Dispose();
        }

        private static JsonDocument Read(string fileName)
        {
            var path = Path.Combine(AppContext.BaseDirectory, "fixtures", fileName);
            Assert.That(File.Exists(path), Is.True,
                $"找不到 fixture：{path}（先在 Kotlin 側跑 SessionIdsFixtureExportTest）");
            return JsonDocument.Parse(File.ReadAllText(path));
        }

        private static List<int> Ints(JsonElement array)
        {
            var list = new List<int>();
            foreach (var item in array.EnumerateArray()) list.Add(item.GetInt32());
            return list;
        }

        private static string Str(JsonElement obj, string name) => obj.GetProperty(name).GetString();

        private static bool Bool(JsonElement obj, string name) => obj.GetProperty(name).GetBoolean();

        [Test]
        public void 每個_case_的_roll_與_unique_都與_Kotlin_版逐欄相同()
        {
            var root = _ids.RootElement;
            Assert.That(SessionIds.Default, Is.EqualTo(Str(root, "dflt")), "出貨預設名與 Kotlin 不同");
            Assert.That(SessionIds.UuidLen, Is.EqualTo(root.GetProperty("uuidLen").GetInt32()), "UuidLen 與 Kotlin 不同");

            var cases = root.GetProperty("cases");
            var count = 0;
            foreach (var testCase in cases.EnumerateArray())
            {
                var name = Str(testCase, "name");
                var current = Str(testCase, "current");
                var script = Ints(testCase.GetProperty("script"));

                // 兩個呼叫各自用**同一份腳本從頭開始**各跑一次（fixture 就是這樣產生的）：
                // 一個實作是物件狀態，共用一份會互相吃掉腳本。
                var rolled = SessionIds.Roll(current, new ScriptedRandom(script));
                var unique = SessionIds.EnsureUnique(current, new ScriptedRandom(script));

                Assert.That(rolled, Is.EqualTo(Str(testCase, "roll")), $"[{name}] roll 不同");
                Assert.That(rolled.Length, Is.EqualTo(testCase.GetProperty("rollLen").GetInt32()),
                    $"[{name}] rollLen 不同");
                Assert.That(Uuid4.IsMatch(rolled), Is.EqualTo(Bool(testCase, "rollIsUuid4")),
                    $"[{name}] rollIsUuid4 不同（實際 {rolled}）");
                Assert.That(rolled.Length > 14 && rolled[14] == '4', Is.EqualTo(Bool(testCase, "rollVersionIs4")),
                    $"[{name}] rollVersionIs4 不同（實際 {rolled}）");
                Assert.That(rolled.Length > 19 && "89ab".IndexOf(rolled[19]) >= 0,
                    Is.EqualTo(Bool(testCase, "rollVariantIn89ab")),
                    $"[{name}] rollVariantIn89ab 不同（實際 {rolled}）");
                Assert.That(rolled != current.Trim(), Is.EqualTo(Bool(testCase, "rollDiffersFromCurrent")),
                    $"[{name}] rollDiffersFromCurrent 不同（實際 {rolled}）");
                Assert.That(unique, Is.EqualTo(Str(testCase, "unique")), $"[{name}] unique 不同");

                count++;
            }

            Assert.That(count, Is.GreaterThan(8), "fixture 太少（匯出那支是不是忘了加 case）");
            TestContext.Out.WriteLine($"SessionIds：{count} 條 case 與 Kotlin 版逐欄相同");
        }

        [Test]
        public void 重_roll_的_case_與_Kotlin_版相同()
        {
            var root = _reroll.RootElement;
            var script = Ints(root.GetProperty("script"));

            // 照 Kotlin 匯出那支的跑法（`SessionIdsFixtureExportTest.匯出重 roll 的真值`）：
            // ①腳本前段造出 first；②拿 first 當 current、**同一串腳本從頭再跑一次** ⇒
            // 第一次候選會 roll 回 first ⇒ 觸發 while 回圈（「按鈕看起來沒反應」的那個 bug），
            // 吃掉腳本後段才吐出 second。⇒ 這條測到的正是「重 roll 不會 roll 回同一個」。
            var first = SessionIds.Roll("", new ScriptedRandom(script));
            var second = SessionIds.Roll(first, new ScriptedRandom(script));

            Assert.That(first, Is.EqualTo(Str(root, "first")), "first 不同");
            Assert.That(second, Is.EqualTo(Str(root, "second")), "second 不同");
            Assert.That(second, Is.EqualTo(Str(root, "expectedSecond")),
                "second 與 expectedSecond 不同（重 roll 沒有照預期吃到第二段腳本）");
            Assert.That(second != first, Is.EqualTo(Bool(root, "differs")), "differs 不同");
            Assert.That(second, Is.Not.EqualTo(first), "重 roll 竟然 roll 回同一個（按鈕看起來會沒反應）");
            Assert.That(Uuid4.IsMatch(second), Is.True, $"重 roll 的結果不是 UUID v4：{second}");

            TestContext.Out.WriteLine($"SessionIds reroll：first={first} ⇒ second={second}");
        }

        [Test]
        public void 舊前綴與空白的判定與_Kotlin_版相同()
        {
            // Kotlin 的 `LEGACY_PREFIXES` 是 private（fixture 只給清單）⇒ 這裡從清單**行為驗**：
            // 「只剩前綴、沒有尾巴」的每一種都必須被換成 UUID。
            var prefixes = new List<string>();
            foreach (var p in _ids.RootElement.GetProperty("legacyPrefixes").EnumerateArray()) prefixes.Add(p.GetString());
            Assert.That(prefixes, Is.Not.Empty, "fixture 沒給 legacyPrefixes");

            foreach (var prefix in prefixes)
            {
                // 腳本用匯出那支的 A 段（每個 nibble 都落在 0..15），照正規方式交值：
                // 出來的候選一定不等於前綴本身 ⇒ 不會進 while 回圈、剛好吃光 31 個值。
                var fixedId = SessionIds.EnsureUnique(prefix, new ScriptedRandom(ScriptA()));
                Assert.That(Uuid4.IsMatch(fixedId), Is.True, $"舊前綴「{prefix}」⇒ 應換成 UUID，實際 {fixedId}");
            }

            // 空白（含「只有空白」）與出貨預設名也一樣要換掉。
            foreach (var blank in new[] { "", "   ", SessionIds.Default })
            {
                var fixedId = SessionIds.EnsureUnique(blank, new ScriptedRandom(ScriptA()));
                Assert.That(Uuid4.IsMatch(fixedId), Is.True, $"「{blank}」⇒ 應換成 UUID，實際 {fixedId}");
            }
        }

        /// <summary>Kotlin 匯出那支的 A 段腳本（`(i * 3 + 1) % 16`，31 個）——剛好夠一次 uuid4 消耗。</summary>
        private static List<int> ScriptA() => Enumerable.Range(0, 31).Select(i => (i * 3 + 1) % 16).ToList();

        /// <summary>
        /// 腳本化亂數：**照 fixture 的 `script` 依呼叫序 pop**；腳本用完就丟例外
        /// （太短時要炸——默默回 0 會讓「消耗順序錯了」變成一個看起來完全正常的 UUID）。
        ///
        /// 為什麼只要「pop 出 n」就等價：Kotlin 側的 ScriptedRandom 交出來的原始值是 `n * 0x11111111`
        /// （每個 nibble 都等於 n），stdlib 的 `nextInt(bound)` 在 bound 是 2 次冪時走
        /// `nextBits(log2(bound))` ⇒ 等於「把低 log2(bound) 位取出來」＝ `n mod bound`。
        /// 所以 C# 端不必複製 Kotlin 的 PRNG，照腳本 pop 出來照同一個規則遮位就會一樣。
        /// （fixture 只用到 bound=16（十六進位）與 bound=4（variant），兩者都落在這條路上。）
        /// </summary>
        private sealed class ScriptedRandom : IRandomSource
        {
            private readonly List<int> _script;
            private int _index;

            public ScriptedRandom(IEnumerable<int> script)
            {
                if (script == null) throw new ArgumentNullException(nameof(script));
                _script = new List<int>(script);
            }

            /// <summary>腳本還剩幾個沒被吃掉（診斷用）。</summary>
            public int Remaining => _script.Count - _index;

            public int NextInt(int bound)
            {
                if (bound <= 0) throw new ArgumentOutOfRangeException(nameof(bound), bound, "bound 必須 > 0");
                var mask = bound - 1;
                if ((bound & mask) != 0)
                    throw new NotSupportedException(
                        $"腳本化亂數只實作 2 次冪的 bound（Kotlin 的非 2 次冪路徑是拒絕取樣）；bound={bound}");
                if (_index >= _script.Count)
                    throw new InvalidOperationException(
                        $"腳本用完（已消耗 {_index} 個，腳本長度 {_script.Count}）——消耗次數與 Kotlin 版不同？");

                var n = _script[_index++];
                var raw = unchecked((uint)n * 0x11111111u);
                return (int)(raw & (uint)mask);
            }
        }
    }
}
