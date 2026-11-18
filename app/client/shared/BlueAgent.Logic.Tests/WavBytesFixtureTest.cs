using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Text.Json;
using BlueAgent.Logic;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// **fixture 對拍**：`fixtures/wav_bytes.json` 是 Kotlin 側（`WavBytes.kt`）由
    /// `WavBytesFixtureExportTest` 機器產生的真值——**呼叫列（calls）＋結果軌跡（trace）**。
    ///
    /// 這裡把同一串呼叫餵進 C# 版逐條重放，與 trace 逐字比對。⇒ 綠＝「同一段 PCM／同一個取樣率
    /// 產出的 44 byte 檔頭與 little-endian 樣本位元組**一個位元組都不差**」。
    ///
    /// 觀測的編碼（兩端同一規則）：位元組陣列 ⇒ **小寫十六進位**（不帶分隔）；丟例外 ⇒ `!THROW`
    /// （只比「會不會丟」，不比例外型別）。
    ///
    /// ⚠️ 這一支**沒有時鐘、沒有檔案**（純算式）⇒ `FakeClock`／記憶體 fs 都不需要。
    ///
    /// fixture 是**產物**，不要手改（要加 case 就改 Kotlin 那支匯出測試再重跑）。
    /// </summary>
    [TestFixture]
    public class WavBytesFixtureTest
    {
        private const int MinCases = 6;

        private JsonDocument _fixture;

        [SetUp]
        public void Load()
        {
            var path = Path.Combine(AppContext.BaseDirectory, "fixtures", "wav_bytes.json");
            Assert.That(File.Exists(path), Is.True,
                $"找不到 fixture：{path}（先在 Kotlin 側跑 WavBytesFixtureExportTest）");
            _fixture = JsonDocument.Parse(File.ReadAllText(path));
        }

        [TearDown]
        public void Unload() => _fixture?.Dispose();

        [Test]
        public void 與_Kotlin_版逐條相同()
        {
            var root = _fixture.RootElement;
            Assert.That(root.GetProperty("module").GetString(), Is.EqualTo("WavBytes"), "fixture 的 module 不對");

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
            TestContext.Out.WriteLine($"wav_bytes.json：{caseCount} 條 case／{total} 步全數相同");
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
                case "encode":
                    return Hex(WavBytes.Encode(BuildPcm(c.GetProperty("pcm")), c.GetProperty("rate").GetInt32()));

                default:
                    return "!UNKNOWN_FN";
            }
        }

        /// <summary>`pcm` 是 Int 的清單 ⇒ `ShortArray`（Kotlin 的 `Int.toShort()`＝切下位 16 bit）。</summary>
        private static short[] BuildPcm(JsonElement vals)
        {
            var outp = new short[vals.GetArrayLength()];
            var i = 0;
            foreach (var v in vals.EnumerateArray()) outp[i++] = unchecked((short)v.GetInt32());
            return outp;
        }

        /// <summary>位元組 ⇒ 小寫十六進位（Kotlin 的 `hex()` 同一規則）。</summary>
        private static string Hex(byte[] b)
        {
            var sb = new StringBuilder(b.Length * 2);
            foreach (var x in b) sb.Append(x.ToString("x2"));
            return sb.ToString();
        }

        private static string FnName(JsonElement call) =>
            call.TryGetProperty("fn", out var v) ? v.GetString() : "?";

        private static string Brief(string s) =>
            s == null ? "null" : (s.Length <= 240 ? s : s.Substring(0, 240) + "…");
    }
}
