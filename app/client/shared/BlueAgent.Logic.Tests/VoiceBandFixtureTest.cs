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
    /// **fixture 對拍**：`fixtures/voice_band.json` 是 Kotlin 那一側（`VoiceBand.kt`）
    /// 由 `VoiceBandFixtureExportTest` 機器產生的真值——內容是**每個 case 的濾波器設定 ＋
    /// 一串 ops ＋ 每一步的輸出樣本**。
    ///
    /// 這裡把**同一批 int16 輸入樣本**餵進 C# 版、用同一串 ops 重放，**逐樣本**比對輸出 ⇒
    /// 綠＝「兩顆 biquad 的係數、定點量化、跨塊狀態在兩邊一模一樣」。
    ///
    /// 觀測的形狀（與 Kotlin 匯出器同一條規則）：
    ///  * `process` ⇒ 就地濾完之後的樣本（逗號分隔）；
    ///  * `step` ⇒ `Step()` 那條 Double 入口的樣本，**逐個量化成 int16**（＝`process` 裡面那行的寫回式）
    ///    ——刻意不記 `Double` 的十進位字串，免得兩邊的 `Double.toString()` 差異混進來；
    ///  * `reset` ⇒ `ok`；`isActive` ⇒ `true`／`false`；`applyTo` ⇒ 回傳的樣本。
    ///
    /// ⚠️ `process` 與 `step` **共用同一份 biquad 狀態**（Kotlin 就是這樣）⇒ 同一個 case 內兩種 op 混用時
    /// 狀態延續；C# 端照樣共用同一顆 <see cref="VoiceBand"/> 實例重放。
    ///
    /// fixture 是**產物**，不要手改（要加 case 就改 Kotlin 那支匯出測試再重跑）。
    /// </summary>
    [TestFixture]
    public class VoiceBandFixtureTest
    {
        private const int MinCases = 10;

        /// <summary>
        /// `VoiceBand` 私有的量化刻度（＝Kotlin 匯出器裡也照抄了一份的 `SCALE`）。這裡只用在
        /// 「把 `Step()` 的 Double 結果寫成 int16」這一步——**與匯出器同一條式子**：
        /// `(y × Scale).coerceIn(-Scale, Scale-1).toInt().toShort()`。
        /// </summary>
        private const double Scale = 32768.0;

        private JsonDocument _fixture;

        [SetUp]
        public void Load()
        {
            var path = Path.Combine(AppContext.BaseDirectory, "fixtures", "voice_band.json");
            Assert.That(File.Exists(path), Is.True,
                $"找不到 fixture：{path}（先在 Kotlin 側跑 VoiceBandFixtureExportTest）");
            _fixture = JsonDocument.Parse(File.ReadAllText(path));
        }

        [TearDown]
        public void Unload() => _fixture?.Dispose();

        [Test]
        public void 逐條重放並比對軌跡()
        {
            var root = _fixture.RootElement;
            Assert.That(root.GetProperty("module").GetString(), Is.EqualTo("VoiceBand"), "fixture 的 module 不對");

            var cases = root.GetProperty("cases");
            var total = 0;

            foreach (var c in cases.EnumerateArray())
            {
                var name = c.GetProperty("name").GetString();
                var ops = c.GetProperty("ops");
                var trace = c.GetProperty("trace");

                Assert.That(trace.GetArrayLength(), Is.EqualTo(ops.GetArrayLength()),
                    $"[{name}] ops 與 trace 的步數不同（fixture 壞了）");

                var band = new VoiceBand(
                    c.GetProperty("sampleRate").GetInt32(),
                    c.GetProperty("highPassHz").GetDouble(),
                    c.GetProperty("lowPassHz").GetDouble(),
                    c.GetProperty("enabled").GetBoolean());

                var actual = Run(band, ops);
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

            var caseCount = cases.GetArrayLength();
            Assert.That(caseCount, Is.GreaterThanOrEqualTo(MinCases), "fixture 太少（匯出那支是不是忘了加 case）");
            TestContext.Out.WriteLine($"VoiceBand：{caseCount} 條 case／{total} 步與 Kotlin 版逐樣本相同");
        }

        // ── 重放器（與 Kotlin 那支 `run()`／`step()` 逐條對應）────────────────────

        private static string OpName(JsonElement op) =>
            op.TryGetProperty("op", out var v) ? v.GetString() : "?";

        private static string Brief(string s) =>
            s == null ? "null" : (s.Length <= 240 ? s : s.Substring(0, 240) + "…");

        private static List<string> Run(VoiceBand band, JsonElement ops)
        {
            var trace = new List<string>();
            foreach (var op in ops.EnumerateArray())
            {
                string r;
                try { r = Step(band, op); }
                catch { r = "!THROW"; }
                trace.Add(r);
            }
            return trace;
        }

        private static string Step(VoiceBand band, JsonElement o)
        {
            var kind = o.GetProperty("op").GetString();
            switch (kind)
            {
                case "process":
                {
                    var samples = Samples(o);
                    band.Process(samples);
                    return Join(samples);
                }
                case "step":
                {
                    var samples = Samples(o);
                    var outp = new List<string>(samples.Length);
                    foreach (var s in samples) outp.Add(Quant(band.Step(s / Scale)).ToString(CultureInfo.InvariantCulture));
                    return string.Join(",", outp);
                }
                case "reset": band.Reset(); return "ok";
                case "isActive": return band.IsActive ? "true" : "false";
                case "applyTo": return Join(band.ApplyTo(Samples(o)));
                default: return "!UNKNOWN_OP:" + kind;
            }
        }

        /// <summary>`(y × Scale).coerceIn(-Scale, Scale-1).toInt().toShort()`——與 `Process` 的寫回式同一條。</summary>
        private static int Quant(double y)
        {
            var v = y * Scale;
            if (v < -Scale) v = -Scale;
            else if (v > Scale - 1.0) v = Scale - 1.0;
            return (short)(int)v;
        }

        private static short[] Samples(JsonElement o)
        {
            var arr = o.GetProperty("samples");
            var outp = new short[arr.GetArrayLength()];
            var i = 0;
            foreach (var v in arr.EnumerateArray()) outp[i++] = (short)v.GetInt32();
            return outp;
        }

        private static string Join(short[] samples)
        {
            var parts = new List<string>(samples.Length);
            foreach (var s in samples) parts.Add(s.ToString(CultureInfo.InvariantCulture));
            return string.Join(",", parts);
        }
    }
}
