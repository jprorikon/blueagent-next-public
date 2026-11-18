using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using BlueAgent.Logic;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// **fixture 對拍**：`fixtures/wake_word.json` は Kotlin 側（`WakeWord.kt`）が
    /// `WakeWordFixtureExportTest` で機械生成した真値——中身は**呼び出し列（calls）＋結果の軌跡（trace）**。
    ///
    /// ここで同じ calls を C# 版に与えて逐條再生し、trace と一字ずつ比べる ⇒ 緑＝「畳み方・近似一致・
    /// 用件の切り出しが両端で同じ」。壊れた入力は両端とも `!THROW`（例外の型名は言語で違うので比べない）。
    ///
    /// 各 trace 文字列の形（Kotlin の匯出側と同じ規則）：
    ///  * `phrases` ⇒ `["ミラ","無月","松木"]`；
    ///  * `label`／`normalized` ⇒ その文字列そのもの；
    ///  * `isFarewell` ⇒ `"true"`／`"false"`；
    ///  * `match` ⇒ `"null"` 或は `{"phrase":…,"rest":…,"has_request":…}`；
    ///  * `fold` ⇒ `{"text":…,"orig":[…]}`。
    ///
    /// ⚠️ `WakeWord` に**時計は無い**（純関数）⇒ ここに `FakeClock`／時間注入は不要。
    ///
    /// ⚠️ 移植陷阱 §1：この fixture は Kotlin の空白語意（U+0085／U+180E は空白でない、U+00A0 は空白）と
    /// `isLetterOrDigit` の境界を**織り込んである**（case `fold_whitespace_and_letters`）。C# 側が
    /// `char.IsWhiteSpace` を使っていたら、そこで落ちる。
    ///
    /// fixture は**産物**、手で直さない（case を足すなら Kotlin の匯出テストを直して再実行）。
    /// </summary>
    [TestFixture]
    public class WakeWordFixtureTest
    {
        private const int MinCases = 18;

        private JsonDocument _fixture;

        [SetUp]
        public void Load()
        {
            var path = Path.Combine(AppContext.BaseDirectory, "fixtures", "wake_word.json");
            Assert.That(File.Exists(path), Is.True,
                $"找不到 fixture：{path}（先在 Kotlin 側跑 WakeWordFixtureExportTest）");
            _fixture = JsonDocument.Parse(File.ReadAllText(path));
        }

        [TearDown]
        public void Unload() => _fixture?.Dispose();

        [Test]
        public void 與_Kotlin_版逐條相同()
        {
            var root = _fixture.RootElement;
            Assert.That(root.GetProperty("module").GetString(), Is.EqualTo("WakeWord"), "fixture 的 module 不對");

            var cases = root.GetProperty("cases");
            var caseCount = 0;
            var total = 0;
            foreach (var c in cases.EnumerateArray())
            {
                var name = c.GetProperty("name").GetString();
                var calls = c.GetProperty("calls");
                var trace = c.GetProperty("trace");

                var actual = Run(calls);
                var expected = new List<string>();
                foreach (var t in trace.EnumerateArray()) expected.Add(t.GetString());

                Assert.That(actual.Count, Is.EqualTo(expected.Count), $"[{name}] 步驟數不符");
                for (var i = 0; i < expected.Count; i++)
                {
                    Assert.That(actual[i], Is.EqualTo(expected[i]),
                        $"[{name}] 第 {i + 1} 步（fn={FnName(calls[i])}）不符：" +
                        $"Kotlin={Brief(expected[i])} / C#={Brief(actual[i])}");
                }
                total += expected.Count;
                caseCount++;
            }
            Assert.That(caseCount, Is.GreaterThanOrEqualTo(MinCases), "fixture 太少（匯出那支是不是忘了加 case）");
            TestContext.Out.WriteLine($"wake_word.json：{caseCount} 條 case／{total} 步全數相同");
        }

        /// <summary>
        /// 重放（Kotlin 匯出那支的 `run()` 逐條對應）。各步が投げれば `!THROW` を記録
        /// （例外の型名は言語で違うので比べない）。
        /// </summary>
        private static List<string> Run(JsonElement calls)
        {
            var trace = new List<string>();
            foreach (var c in calls.EnumerateArray())
            {
                string r;
                try { r = Step(c); }
                catch { r = "!THROW"; }
                trace.Add(r);
            }
            return trace;
        }

        private static string Step(JsonElement c)
        {
            switch (c.GetProperty("fn").GetString())
            {
                case "phrases":
                    return JArrayOfStrings(WakeWord.Phrases(Str(c, "character"), Str(c, "custom"))).ToString();

                case "label":
                    return WakeWord.Label(Str(c, "character"), Str(c, "custom"));

                case "isFarewell":
                    return WakeWord.IsFarewell(Str(c, "text")) ? "true" : "false";

                case "match":
                    return ObsHit(WakeWord.Match(Str(c, "text"), PhrasesOf(c)));

                case "normalized":
                    return WakeWord.Normalized(Str(c, "text"));

                case "fold":
                    return ObsFolded(WakeWord.Fold(Str(c, "text")));

                default:
                    return "!UNKNOWN_FN";
            }
        }

        private static JArray JArrayOfStrings(IReadOnlyList<string> items)
        {
            var a = new JArray();
            foreach (var s in items) a.Put(s);
            return a;
        }

        private static List<string> PhrasesOf(JsonElement c)
        {
            var list = new List<string>();
            foreach (var e in c.GetProperty("phrases").EnumerateArray()) list.Add(e.GetString());
            return list;
        }

        /// <summary>`Hit?` ⇒ `"null"` 或は `{"phrase":…,"rest":…,"has_request":…}`（緊湊 JSON）。</summary>
        private static string ObsHit(WakeWord.Hit h)
        {
            if (h == null) return "null";
            return new JObject()
                .Put("phrase", h.Phrase)
                .Put("rest", h.Rest)
                .Put("has_request", h.HasRequest)
                .ToString();
        }

        /// <summary>`Folded` ⇒ `{"text":…,"orig":[…]}`——`orig` は rest の切り出し位置そのもの。</summary>
        private static string ObsFolded(WakeWord.Folded f)
        {
            var orig = new JArray();
            foreach (var i in f.Orig) orig.Put(i);
            return new JObject().Put("text", f.Text).Put("orig", orig).ToString();
        }

        private static string Str(JsonElement c, string name) => c.GetProperty(name).GetString();

        private static string FnName(JsonElement call) =>
            call.TryGetProperty("fn", out var v) ? v.GetString() : "?";

        /// <summary>失敗メッセージ用の短縮（trace は長くなり得る）。</summary>
        private static string Brief(string s) =>
            s == null ? "null" : (s.Length <= 240 ? s : s.Substring(0, 240) + "…");
    }
}
