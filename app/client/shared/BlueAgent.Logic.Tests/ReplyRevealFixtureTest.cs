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
    /// **fixture 對拍**：`fixtures/reply_reveal.json` 是 Kotlin 那一側（`ReplyReveal.kt`）
    /// 由 `ReplyRevealFixtureExportTest` 機器產生的真值——內容是**事件序列（events）＋每一步的觀測（steps）**。
    ///
    /// 這裡把同一串事件餵進 C# 版逐條重放，每一步與 steps 逐欄比對
    /// （changed／shown／parked／closed／closedFinal／bubbles）⇒ 綠＝「C# 版與 Kotlin 版在這批 case 上行為相同」。
    /// `reset` 那一步沒有回傳值（不比 changed，其餘照比）。
    ///
    /// fixture 是**產物**，不要手改（要加 case 就改 Kotlin 那個匯出測試再重跑）。
    /// </summary>
    [TestFixture]
    public class ReplyRevealFixtureTest
    {
        private JsonDocument _fixture;

        [SetUp]
        public void Load()
        {
            var path = Path.Combine(AppContext.BaseDirectory, "fixtures", "reply_reveal.json");
            Assert.That(File.Exists(path), Is.True, $"找不到 fixture：{path}（先在 Kotlin 側跑匯出）");
            _fixture = JsonDocument.Parse(File.ReadAllText(path));
        }

        [TearDown]
        public void Unload() => _fixture?.Dispose();

        /// <summary>這條 fixture 的 case 數下限（匯出那支是不是忘了加 case）。</summary>
        private const int MinCases = 10;

        [Test]
        public void 逐條重放並比對每一步()
        {
            var cases = _fixture.RootElement.GetProperty("cases");
            var totalSteps = 0;

            foreach (var c in cases.EnumerateArray())
            {
                var name = c.GetProperty("name").GetString();
                var max = c.GetProperty("maxSentences").GetInt32();
                var msPerChar = c.GetProperty("msPerChar").GetInt32();
                var tickMs = c.GetProperty("tickMs").GetInt64();
                var events = c.GetProperty("events");
                var steps = c.GetProperty("steps");

                Assert.That(steps.GetArrayLength(), Is.EqualTo(events.GetArrayLength()),
                    $"[{name}] events 與 steps 的步數不同（fixture 壞了）");

                var reveal = new ReplyReveal(max, msPerChar, tickMs);

                for (var i = 0; i < steps.GetArrayLength(); i++)
                {
                    var ev = events[i];
                    var step = steps[i];
                    var kind = ev.GetProperty("e").GetString();
                    var stepNo = i + 1;

                    Assert.That(step.GetProperty("e").GetString(), Is.EqualTo(kind),
                        $"[{name}] 第 {stepNo} 步：steps[i].e 與 events[i].e 不同（fixture 壞了）");

                    bool? changed = null;
                    switch (kind)
                    {
                        case "text":
                            changed = reveal.SetText(ev.GetProperty("text").GetString());
                            break;
                        case "speech":
                            changed = reveal.OnSpeech(Seconds(ev));
                            break;
                        case "tick":
                            changed = reveal.Tick();
                            break;
                        case "reset":
                            reveal.Reset();       // 沒有回傳值 ⇒ 不比 changed
                            break;
                        default:
                            Assert.Fail($"[{name}] 第 {stepNo} 步：不認識的事件種類「{kind}」");
                            break;
                    }

                    if (step.TryGetProperty("changed", out var changedEl))
                    {
                        // reset 那一步沒有回傳值；有 changed 的步驟必定有回傳值。
                        Assert.That(changed.HasValue, Is.True,
                            $"[{name}] 第 {stepNo} 步（{kind}）：fixture 有 changed 但這個事件沒有回傳值");
                        Assert.That(changed.Value, Is.EqualTo(changedEl.GetBoolean()),
                            $"[{name}] 第 {stepNo} 步（{kind}）：changed 不同（Kotlin={changedEl.GetBoolean()} / C#={changed.Value}）");
                    }

                    CheckField(name, stepNo, kind, "shown", step, "shown", reveal.ShownCount);
                    CheckField(name, stepNo, kind, "parked", step, "parked", reveal.ParkedCount);
                    CheckField(name, stepNo, kind, "closed", step, "closed", reveal.ClosedCount(false));
                    CheckField(name, stepNo, kind, "closedFinal", step, "closedFinal", reveal.ClosedCount(true));

                    var expectedBubbles = Strings(step.GetProperty("bubbles"));
                    var actualBubbles = reveal.Bubbles();
                    Assert.That(actualBubbles, Is.EqualTo(expectedBubbles),
                        $"[{name}] 第 {stepNo} 步（{kind}）：bubbles 不同" +
                        $"（Kotlin=[{string.Join(" | ", expectedBubbles)}] / C#=[{string.Join(" | ", actualBubbles)}]）");

                    totalSteps++;
                }
            }

            var caseCount = cases.GetArrayLength();
            Assert.That(caseCount, Is.GreaterThanOrEqualTo(MinCases), "fixture 太少（匯出那支是不是忘了加 case）");
            TestContext.Out.WriteLine($"ReplyReveal：{caseCount} 條 case／{totalSteps} 步與 Kotlin 版相同");
        }

        /// <summary>
        /// 這條不變式（`ReplyReveal.kt` 類頭記載的那場真實事故）：**顯示過的段落只增不減**。
        /// 文字一段段長、語音一幀幀到的過程中，任何一個事件都不得讓畫面往後退。
        /// 只有兩種「歸零」是合法的：`reset`（換下一個回覆）與文字被清空（這一輪沒有東西可顯示）。
        /// fixture 裡的 `owner_bug_regroup_never_shrinks` 就是重演事故的那條（重新分段讓 shown 從 3 打回 1）。
        /// </summary>
        [Test]
        public void 顯示過的段落只增不減()
        {
            foreach (var c in _fixture.RootElement.GetProperty("cases").EnumerateArray())
            {
                var name = c.GetProperty("name").GetString();
                var max = c.GetProperty("maxSentences").GetInt32();
                var msPerChar = c.GetProperty("msPerChar").GetInt32();
                var tickMs = c.GetProperty("tickMs").GetInt64();

                var reveal = new ReplyReveal(max, msPerChar, tickMs);
                var events = c.GetProperty("events");

                for (var i = 0; i < events.GetArrayLength(); i++)
                {
                    var ev = events[i];
                    var kind = ev.GetProperty("e").GetString();
                    var stepNo = i + 1;
                    var before = reveal.ShownCount;

                    if (kind == "reset")
                    {
                        reveal.Reset();               // 換下一個回覆：這一輪重新開始，歸零是對的
                        continue;
                    }

                    if (kind == "text")
                    {
                        var text = ev.GetProperty("text").GetString();
                        reveal.SetText(text);
                        // 文字被清空 ⇒ 這一輪沒有東西可顯示，畫面清空也是對的（不是「縮水」）。
                        if (ReplySplit.Groups(text, max).Count == 0) continue;
                    }
                    else if (kind == "speech")
                    {
                        reveal.OnSpeech(Seconds(ev));
                    }
                    else
                    {
                        reveal.Tick();
                    }

                    Assert.That(reveal.ShownCount, Is.GreaterThanOrEqualTo(before),
                        $"[{name}] 第 {stepNo} 步（{kind}）：顯示過的段落只增不減" +
                        $"（before={before} → after={reveal.ShownCount}）");
                }
            }
        }

        /// <summary>`Bubbles()` 回的是副本：外殼（或測試）改它不能動到內部狀態。</summary>
        [Test]
        public void Bubbles_回的是副本()
        {
            var reveal = new ReplyReveal(3, 100, 50L);
            reveal.SetText("あ。い。う。え。");

            var copy = reveal.Bubbles();
            Assert.That(copy, Is.EqualTo(new List<string> { "あ。い。う。" }), "第一條沒顯示出來");
            copy.Clear();

            Assert.That(reveal.Bubbles(), Is.EqualTo(new List<string> { "あ。い。う。" }),
                "Bubbles() 必須回副本：改回傳的清單不能動到內部狀態");
        }

        private static void CheckField(string name, int stepNo, string kind, string label,
                                       JsonElement step, string key, int actual)
        {
            var expected = step.GetProperty(key).GetInt32();
            Assert.That(actual, Is.EqualTo(expected),
                $"[{name}] 第 {stepNo} 步（{kind}）：{label} 不同（Kotlin={expected} / C#={actual}）");
        }

        /// <summary>`seconds` 在 fixture 裡是字串（例如 `"0.5"`）；也容得下數字（兩種都吃）。</summary>
        private static double Seconds(JsonElement ev)
        {
            var el = ev.GetProperty("seconds");
            return el.ValueKind == JsonValueKind.String
                ? double.Parse(el.GetString(), CultureInfo.InvariantCulture)
                : el.GetDouble();
        }

        private static List<string> Strings(JsonElement array)
        {
            var list = new List<string>();
            foreach (var item in array.EnumerateArray()) list.Add(item.GetString());
            return list;
        }
    }
}
