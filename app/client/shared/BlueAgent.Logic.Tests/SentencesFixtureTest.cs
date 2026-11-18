using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using BlueAgent.Logic;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// **fixture 對拍**：`fixtures/sentences.json` 是 Kotlin 那一側（`Sentences.kt`）
    /// 由機器產生的真值；這裡把同一批輸入餵進 C# 版，逐欄比對。
    ///
    /// 每一條 case 驗兩件事：
    ///  * `out` —— `Sentences.Split(text, maxChars, softMin)` 的結果；
    ///  * `steps` —— 把 text 按 `chunk` 大小切塊逐次餵給 `Sentences.Stream(maxChars, softMin)`，
    ///    每一步回傳的段；最後一步是 `Flush()` 的返回。（`chunk <= 0` 表示一次把整段餵進去；
    ///    注意即使是空字串也要餵一次再 flush，見 case `empty` 的 steps 是 `[[],[]]`。）
    ///
    /// ⇒ 這一支綠＝「C# 版與 Kotlin 版在這批 case 上行為相同」。fixture 是**產物**，
    /// 不要手改（要加 case 就改 Kotlin 那個匯出測試再重跑）。
    /// </summary>
    [TestFixture]
    public class SentencesFixtureTest
    {
        private JsonDocument _fixture;

        [SetUp]
        public void Load()
        {
            var path = Path.Combine(AppContext.BaseDirectory, "fixtures", "sentences.json");
            Assert.That(File.Exists(path), Is.True, $"找不到 fixture：{path}（先在 Kotlin 側跑匯出）");
            _fixture = JsonDocument.Parse(File.ReadAllText(path));
        }

        [TearDown]
        public void Unload() => _fixture?.Dispose();

        private static List<string> Strings(JsonElement array)
        {
            var list = new List<string>();
            foreach (var item in array.EnumerateArray()) list.Add(item.GetString());
            return list;
        }

        /// <summary>JSON 的「陣列的陣列」→ `List<List<string>>`（給 steps 用）。</summary>
        private static List<List<string>> StringLists(JsonElement array)
        {
            var list = new List<List<string>>();
            foreach (var item in array.EnumerateArray()) list.Add(Strings(item));
            return list;
        }

        [Test]
        public void 與_Kotlin_版逐條相同()
        {
            var cases = _fixture.RootElement.GetProperty("cases");
            var count = 0;
            var stepsTotal = 0;
            foreach (var testCase in cases.EnumerateArray())
            {
                var name = testCase.GetProperty("name").GetString();
                var text = testCase.GetProperty("text").GetString();
                var maxChars = testCase.GetProperty("maxChars").GetInt32();
                var softMin = testCase.GetProperty("softMin").GetInt32();
                var chunk = testCase.GetProperty("chunk").GetInt32();
                var expectedOut = Strings(testCase.GetProperty("out"));
                var expectedSteps = StringLists(testCase.GetProperty("steps"));

                // ① out：一次把整段切完的結果
                var actualOut = Sentences.Split(text, maxChars, softMin);
                Assert.That(actualOut, Is.EqualTo(expectedOut), $"[{name}] out（Split）不同");

                // ② steps：逐塊餵 + flush，逐步比對
                // 切塊方式與 Kotlin 匯出器一致：`chunk <= 0` ⇒ 整段一次（**連空字串也算一塊**，
                // 這就是 case `empty` 的 steps 是 [[],[]] 的原因）；否則按 chunk 大小切。
                var stream = new Sentences.Stream(maxChars, softMin);
                var actualSteps = new List<List<string>>();
                List<string> chunks;
                if (chunk <= 0)
                {
                    chunks = new List<string> { text };
                }
                else
                {
                    chunks = new List<string>();
                    for (var i = 0; i < text.Length; i += chunk)
                        chunks.Add(text.Substring(i, Math.Min(chunk, text.Length - i)));
                }
                if (chunks.Count == 0)
                {
                    actualSteps.Add(stream.Flush());
                }
                else
                {
                    foreach (var piece in chunks) actualSteps.Add(stream.Feed(piece));
                    actualSteps.Add(stream.Flush());
                }

                Assert.That(actualSteps.Count, Is.EqualTo(expectedSteps.Count),
                    $"[{name}] steps 步數不同（實際 {actualSteps.Count}，預期 {expectedSteps.Count}）");
                for (var step = 0; step < expectedSteps.Count; step++)
                {
                    Assert.That(actualSteps[step], Is.EqualTo(expectedSteps[step]),
                        $"[{name}] 第 {step} 步（0 起算）不同");
                }

                count++;
                stepsTotal += expectedSteps.Count;
            }
            Assert.That(count, Is.GreaterThan(20), "fixture 太少（匯出那支是不是忘了加 case）");
            TestContext.Out.WriteLine($"Sentences：{count} 條 case／{stepsTotal} 步全數相同");
        }
    }
}
