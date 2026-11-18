using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using BlueAgent.Logic;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// **fixture 對拍**：`fixtures/gate_window.json` は Kotlin 側（`GateWindow.kt`）が
    /// `GateWindowFixtureExportTest` で機械生成した真値——**呼び出し列（calls）＋結果の軌跡（trace）**。
    ///
    /// ここで同じ calls を C# 版に与えて逐條再生し、trace と一字ずつ比べる ⇒ 緑＝「窓の選び方・
    /// 前詰めのパディング・PCM 変換の飽和が両端で同じ」。
    ///
    /// **配列の符号化**（両端で同一）：
    ///  * 入力は巨大になり得る（窓は 24000 要素）⇒ コンパクトな仕様で渡す：
    ///    `literal`（小配列をそのまま）／`marks`（`len` 個を `value` で埋め、`marks=[i,v]` だけ差し替え）；
    ///  * `window` の出力（常に 24000 個）は**可逆な RLE** ＝ 等しい値の連なりを `数:値` に畳む
    ///    （`,` 区切り）。値の文字列化は**庫の変換**（`JObject.Put(Double).OptString`＝Java の
    ///    `Double.toString`）——テスト側で二つ目のフォーマッタを書かない、の規律；
    ///  * `pcm16` ⇒ 16-bit 整数の緊湊 JSON；`floatsOfPcm16` ⇒ float の緊湊 JSON（`samples` が小さいので短い）；
    ///  * `starts` ⇒ 整数の緊湊 JSON；`keptName` ⇒ その文字列；
    ///  * `windowIsCopy` ⇒ **一つの** Double 文字列（窓がコピーであることの観測）。
    ///
    /// ⚠️ `GateWindow` に**時計は無い**（入力はスパン長・窓・PCM だけ）⇒ `FakeClock`／時間注入は不要。
    ///
    /// fixture は**産物**、手で直さない（case を足すなら Kotlin の匯出テストを直して再実行）。
    /// </summary>
    [TestFixture]
    public class GateWindowFixtureTest
    {
        private const int MinCases = 16;

        private JsonDocument _fixture;

        [SetUp]
        public void Load()
        {
            var path = Path.Combine(AppContext.BaseDirectory, "fixtures", "gate_window.json");
            Assert.That(File.Exists(path), Is.True,
                $"找不到 fixture：{path}（先在 Kotlin 側跑 GateWindowFixtureExportTest）");
            _fixture = JsonDocument.Parse(File.ReadAllText(path));
        }

        [TearDown]
        public void Unload() => _fixture?.Dispose();

        [Test]
        public void 與_Kotlin_版逐條相同()
        {
            var root = _fixture.RootElement;
            Assert.That(root.GetProperty("module").GetString(), Is.EqualTo("GateWindow"), "fixture 的 module 不對");

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
            TestContext.Out.WriteLine($"gate_window.json：{caseCount} 條 case／{total} 步全數相同");
        }

        // ── 重放器（Kotlin 匯出那支的 `run()` 逐條對應）────────────────────────────

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
                case "starts":
                    return ObsInts(GateWindow.Starts(c.GetProperty("span").GetInt32(), Str(c, "rule")));

                case "window":
                    return ObsRle(GateWindow.Window(BuildSpan(c.GetProperty("span")), c.GetProperty("start").GetInt32()));

                case "windowIsCopy":
                {
                    var span = BuildSpan(c.GetProperty("span"));
                    var w = GateWindow.Window(span, c.GetProperty("start").GetInt32());
                    w[0] = -99f;                                  // コピーでなければ span が汚れる
                    return DStr(span[0]);
                }

                case "pcm16":
                    return ObsShorts(GateWindow.Pcm16(BuildSpan(c.GetProperty("span"))));

                case "floatsOfPcm16":
                {
                    var samples = c.TryGetProperty("samples", out var s)
                        ? s.GetInt32()
                        : GateWindow.WindowSamples;
                    return ObsDoubles(GateWindow.FloatsOfPcm16(BuildPcm(c.GetProperty("pcm")), samples));
                }

                case "keptName":
                    return GateWindow.KeptName(c.GetProperty("index").GetInt32());

                default:
                    return "!UNKNOWN_FN";
            }
        }

        // ── 入力仕様 → 実配列（Kotlin の buildSpan／buildPcm と同じ規則）──────────────

        private static float[] BuildSpan(JsonElement spec)
        {
            switch (spec.GetProperty("kind").GetString())
            {
                case "literal":
                {
                    var vals = spec.GetProperty("values");
                    var outp = new float[vals.GetArrayLength()];
                    var i = 0;
                    foreach (var v in vals.EnumerateArray()) outp[i++] = (float)v.GetDouble();
                    return outp;
                }

                case "marks":
                {
                    var n = spec.GetProperty("len").GetInt32();
                    var v = (float)spec.GetProperty("value").GetDouble();
                    var outp = new float[n];
                    for (var i = 0; i < n; i++) outp[i] = v;
                    foreach (var m in spec.GetProperty("marks").EnumerateArray())
                    {
                        var pair = m.EnumerateArray().ToArray();
                        outp[pair[0].GetInt32()] = (float)pair[1].GetDouble();
                    }
                    return outp;
                }

                default:
                    throw new ArgumentException("bad span kind");
            }
        }

        private static short[] BuildPcm(JsonElement spec)
        {
            var vals = spec.GetProperty("values");
            var outp = new short[vals.GetArrayLength()];
            var i = 0;
            // unchecked：Kotlin の `Int.toShort()`（下位 16-bit に切る）と同じ。fixture の値は範囲内。
            foreach (var v in vals.EnumerateArray()) outp[i++] = unchecked((short)v.GetInt32());
            return outp;
        }

        // ── 観測の符号化 ────────────────────────────────────────────────────────

        private static string ObsInts(int[] arr)
        {
            var a = new JArray();
            foreach (var v in arr) a.Put(v);
            return a.ToString();
        }

        private static string ObsShorts(short[] arr)
        {
            var a = new JArray();
            foreach (var v in arr) a.Put((int)v);
            return a.ToString();
        }

        private static string ObsDoubles(float[] arr)
        {
            var a = new JArray();
            foreach (var v in arr) a.Put((double)v);
            return a.ToString();
        }

        /// <summary>float 配列の可逆な RLE（Kotlin の `obsFloats` と同じ規則・同じ値の文字列化）。</summary>
        private static string ObsRle(float[] arr)
        {
            var sb = new StringBuilder();
            var i = 0;
            while (i < arr.Length)
            {
                var j = i;
                while (j + 1 < arr.Length && arr[j + 1] == arr[i]) j++;
                if (sb.Length > 0) sb.Append(',');
                sb.Append(j - i + 1).Append(':').Append(DStr(arr[i]));
                i = j + 1;
            }
            return sb.ToString();
        }

        /// <summary>1 つの Double を Java の `Double.toString` の形で（庫の変換を使う）。</summary>
        private static string DStr(double d) => new JObject().Put("x", d).OptString("x");

        private static string Str(JsonElement c, string name) => c.GetProperty(name).GetString();

        private static string FnName(JsonElement call) =>
            call.TryGetProperty("fn", out var v) ? v.GetString() : "?";

        private static string Brief(string s) =>
            s == null ? "null" : (s.Length <= 240 ? s : s.Substring(0, 240) + "…");
    }
}
