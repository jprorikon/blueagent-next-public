using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using BlueAgent.Logic;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// **fixture 對拍**：`fixtures/reply_split.json` 是 Kotlin 那一側（`ReplySplit.kt`）
    /// 由 `LogicFixtureExportTest` 機器產生的真值；這裡把同一批輸入餵進 C# 版，逐欄比對。
    ///
    /// ⇒ 這一支綠＝「C# 版與 Kotlin 版在這批 case 上行為相同」。fixture 是**產物**，
    /// 不要手改（要加 case 就改 Kotlin 那個匯出測試再重跑）。
    /// </summary>
    [TestFixture]
    public class ReplySplitFixtureTest
    {
        private JsonDocument _fixture;

        [SetUp]
        public void Load()
        {
            var path = Path.Combine(AppContext.BaseDirectory, "fixtures", "reply_split.json");
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

        private static List<long> Longs(JsonElement array)
        {
            var list = new List<long>();
            foreach (var item in array.EnumerateArray()) list.Add(item.GetInt64());
            return list;
        }

        [Test]
        public void 與_Kotlin_版逐條相同()
        {
            var cases = _fixture.RootElement.GetProperty("cases");
            var count = 0;
            foreach (var testCase in cases.EnumerateArray())
            {
                var name = testCase.GetProperty("name").GetString();
                var input = testCase.GetProperty("in");
                var expected = testCase.GetProperty("out");
                var text = input.GetProperty("text").GetString();
                var max = input.GetProperty("max").GetInt32();
                var msPerChar = input.GetProperty("msPerChar").GetInt32();

                var groups = ReplySplit.Groups(text, max);
                Assert.That(groups, Is.EqualTo(Strings(expected.GetProperty("groups"))),
                    $"[{name}] groups 不同");
                Assert.That(ReplySplit.BoundsMs(groups, msPerChar), Is.EqualTo(Longs(expected.GetProperty("boundsMs"))),
                    $"[{name}] boundsMs 不同");
                Assert.That(ReplySplit.Chars(text), Is.EqualTo(expected.GetProperty("chars").GetInt32()),
                    $"[{name}] chars 不同");
                Assert.That(ReplySplit.Speakable(text), Is.EqualTo(expected.GetProperty("speakable").GetBoolean()),
                    $"[{name}] speakable 不同");
                Assert.That(ReplySplit.Sentences(text), Is.EqualTo(Strings(expected.GetProperty("sentences"))),
                    $"[{name}] sentences 不同");
                count++;
            }
            Assert.That(count, Is.GreaterThan(20), "fixture 太少（匯出那支是不是忘了加 case）");
            TestContext.Out.WriteLine($"ReplySplit：{count} 條 case 與 Kotlin 版相同");
        }
    }
}
