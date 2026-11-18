using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using BlueAgent.Logic.Engine;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// **fixture 對拍（M4：情緒原型表 ＋ 演出總譜）**：`fixtures/engine_expression.json` 由 Kotlin 側
    /// `EngineEmotionExpressionFixtureExportTest` 錄製。
    ///
    /// 原型表是**整表 dump** 當抄寫的守門員（那是 generated data，手抄 200 行一定抄錯）——
    /// C# 那份 `EmotionArchetypes.cs` 也是機器產的（`tools/gen_emotion_archetypes_cs.py`），
    /// 兩邊都由機器保證；這一支測試則保證「產生器的輸出」與「引擎實際讀到的表」一致。
    /// </summary>
    [TestFixture]
    public class EngineExpressionFixtureTest
    {
        private JsonDocument _fixture;
        private float _tolerance;

        [SetUp]
        public void Load()
        {
            var path = Path.Combine(AppContext.BaseDirectory, "fixtures", "engine_expression.json");
            Assert.That(File.Exists(path), Is.True, $"找不到 fixture：{path}（先在 Kotlin 側跑 `gradle :engine:test`）");
            _fixture = JsonDocument.Parse(File.ReadAllText(path));
            _tolerance = _fixture.RootElement.GetProperty("tolerance").GetSingle();
        }

        [TearDown]
        public void Unload() => _fixture?.Dispose();

        // ── ① 原型表整表（含順序、範圍、tags）────────────────────────────────────

        [Test]
        public void 原型表整表相同()
        {
            var archetypes = _fixture.RootElement.GetProperty("archetypes");
            Assert.That(EmotionArchetypes.List.Count, Is.EqualTo(archetypes.GetArrayLength()), "情緒數量");

            var i = 0;
            var variants = 0;
            foreach (var a in archetypes.EnumerateArray())
            {
                var expected = EmotionArchetypes.List[i];
                var ctx = $"[{i}] {expected.Key}";
                Assert.That(expected.Key, Is.EqualTo(a.GetProperty("emotion").GetString()), ctx + " 名字與順序");

                var baseTendency = a.GetProperty("baseTendency");
                AssertKeyOrder(expected.Value.BaseTendencyKeys, baseTendency, ctx + " baseTendency 鍵序");
                foreach (var p in baseTendency.EnumerateObject())
                {
                    AssertRange(expected.Value.BaseTendency[p.Name], p.Value, $"{ctx}.{p.Name}");
                }

                var expVariants = a.GetProperty("variants");
                Assert.That(expected.Value.VariantKeys.Count, Is.EqualTo(expVariants.GetArrayLength()), ctx + " 變體數");
                var j = 0;
                foreach (var v in expVariants.EnumerateArray())
                {
                    var vname = v.GetProperty("name").GetString();
                    Assert.That(expected.Value.VariantKeys[j], Is.EqualTo(vname), $"{ctx} 變體順序[{j}]");
                    var actualVariant = expected.Value.Variants[vname];

                    var ranges = v.GetProperty("ranges");
                    AssertKeyOrder(actualVariant.RangeKeys, ranges, $"{ctx}.{vname} ranges 鍵序");
                    foreach (var p in ranges.EnumerateObject())
                    {
                        AssertRange(actualVariant.Ranges[p.Name], p.Value, $"{ctx}.{vname}.{p.Name}");
                    }

                    var tags = v.GetProperty("tags");
                    Assert.That(actualVariant.Tags.Count, Is.EqualTo(tags.GetArrayLength()), $"{ctx}.{vname} tags 數");
                    var k = 0;
                    foreach (var t in tags.EnumerateArray())
                    {
                        Assert.That(actualVariant.Tags[k], Is.EqualTo(t.GetString()), $"{ctx}.{vname} tags[{k}]");
                        k++;
                    }
                    variants++;
                    j++;
                }
                i++;
            }
            TestContext.Out.WriteLine($"原型表：{i} 個情緒／{variants} 個變體（含順序、範圍、tags）全數相同");
        }

        // ── ② 演出總譜：同一顆 seed 生出的 timeline／peakFACS／idleBias ──────────

        [Test]
        public void 演出總譜逐條重放()
        {
            var generator = new RuntimeExpressionGenerator();
            var n = 0;
            foreach (var c in _fixture.RootElement.GetProperty("cases").EnumerateArray())
            {
                var input = ReadInput(c.GetProperty("in"));
                var expression = generator.Generate(input);
                var exp = c.GetProperty("out");
                var ctx = $"case#{n}({input.Emotion}/{exp.GetProperty("variant").GetString()})";

                Assert.That(expression.Emotion, Is.EqualTo(exp.GetProperty("emotion").GetString()), ctx + " emotion");
                Assert.That(expression.Variant, Is.EqualTo(exp.GetProperty("variant").GetString()), ctx + " variant");
                AssertClose(expression.Intensity, exp.GetProperty("intensity").GetDouble(), ctx + " intensity");
                AssertMap(expression.PeakFACS, exp.GetProperty("peakFACS"), ctx + " peakFACS");
                AssertMap(expression.IdleBias, exp.GetProperty("idleBias"), ctx + " idleBias");
                AssertClose(expression.RecoveryDuration, exp.GetProperty("recoveryDuration").GetDouble(), ctx + " recoveryDuration");

                var expTimeline = exp.GetProperty("timeline");
                Assert.That(expression.Timeline.Count, Is.EqualTo(expTimeline.GetArrayLength()), ctx + " timeline 幀數");
                var f = 0;
                foreach (var frame in expTimeline.EnumerateArray())
                {
                    var actual = expression.Timeline[f];
                    AssertClose(actual.Time, frame.GetProperty("time").GetSingle(), $"{ctx} timeline[{f}].time");
                    AssertClose(actual.Duration, frame.GetProperty("duration").GetSingle(), $"{ctx} timeline[{f}].duration");
                    Assert.That(actual.Easing.ToString(), Is.EqualTo(ToCsName(frame.GetProperty("easing").GetString())),
                        $"{ctx} timeline[{f}].easing");
                    AssertClose(actual.Weight, frame.GetProperty("weight").GetSingle(), $"{ctx} timeline[{f}].weight");
                    AssertMap(actual.Facs, frame.GetProperty("facs"), $"{ctx} timeline[{f}].facs");
                    f++;
                }

                var expDuration = c.GetProperty("duration").GetSingle();
                AssertClose(ExpressionTimeline.Duration(expression.Timeline), expDuration, ctx + " duration");

                foreach (var sample in c.GetProperty("samples").EnumerateArray())
                {
                    var elapsed = sample.GetProperty("elapsed").GetSingle();
                    var actual = ExpressionTimeline.Evaluate(expression.Timeline, elapsed);
                    AssertMap(actual, sample.GetProperty("facs"), $"{ctx} @{elapsed}");
                }
                n++;
            }
            TestContext.Out.WriteLine($"演出總譜：{n} 條（含 timeline、peakFACS、idleBias、逐時刻求值）全數相同");
        }

        // ── ③ 時間軸求值本身（手寫關鍵影格，與生成器解耦）────────────────────────

        [Test]
        public void 時間軸求值相同()
        {
            var n = 0;
            foreach (var c in _fixture.RootElement.GetProperty("timelineCases").EnumerateArray())
            {
                var timeline = new List<RuntimeExpressionKeyframe>();
                foreach (var frame in c.GetProperty("keyframes").EnumerateArray())
                {
                    timeline.Add(new RuntimeExpressionKeyframe(
                        frame.GetProperty("time").GetSingle(),
                        frame.GetProperty("duration").GetSingle(),
                        ParseEasing(frame.GetProperty("easing").GetString()),
                        ReadMap(frame.GetProperty("facs")),
                        frame.GetProperty("weight").GetSingle()));
                }

                var ctx = $"timelineCase#{n}";
                AssertClose(ExpressionTimeline.Duration(timeline), c.GetProperty("duration").GetSingle(), ctx + " duration");
                foreach (var sample in c.GetProperty("samples").EnumerateArray())
                {
                    var elapsed = sample.GetProperty("elapsed").GetSingle();
                    AssertMap(ExpressionTimeline.Evaluate(timeline, elapsed), sample.GetProperty("facs"), $"{ctx} @{elapsed}");
                }
                n++;
            }
            TestContext.Out.WriteLine($"時間軸：{n} 組關鍵影格全數相同");
        }

        // ── 讀取／比對小工具 ──────────────────────────────────────────────────

        private static string ToCsName(string kotlinName)
        {
            switch (kotlinName)
            {
                case "LINEAR": return "Linear";
                case "EASE_IN": return "EaseIn";
                case "EASE_OUT": return "EaseOut";
                case "EASE_IN_OUT": return "EaseInOut";
                default: throw new AssertionException("不認得的 easing：" + kotlinName);
            }
        }

        private static EasingName ParseEasing(string kotlinName)
        {
            switch (kotlinName)
            {
                case "LINEAR": return EasingName.Linear;
                case "EASE_IN": return EasingName.EaseIn;
                case "EASE_OUT": return EasingName.EaseOut;
                case "EASE_IN_OUT": return EasingName.EaseInOut;
                default: throw new AssertionException("不認得的 easing：" + kotlinName);
            }
        }

        private static ExpressionGenerateInput ReadInput(JsonElement el)
        {
            CharacterPersonality personality = null;
            if (el.TryGetProperty("personality", out var p))
            {
                personality = new CharacterPersonality(
                    p.GetProperty("expressiveness").GetDouble(),
                    p.GetProperty("softness").GetDouble(),
                    p.GetProperty("shyness").GetDouble(),
                    p.GetProperty("gazeStability").GetDouble());
            }
            var tags = new List<string>();
            foreach (var t in el.GetProperty("contextTags").EnumerateArray()) tags.Add(t.GetString());

            return new ExpressionGenerateInput(
                emotion: el.GetProperty("emotion").GetString(),
                variant: el.TryGetProperty("variant", out var v) ? v.GetString() : null,
                intensity: el.GetProperty("intensity").GetDouble(),
                contextTags: tags,
                personality: personality,
                previousState: ReadMap(el.GetProperty("previousState")),
                seed: el.GetProperty("seed").GetDouble());
        }

        private static Dictionary<string, float> ReadMap(JsonElement el)
        {
            var outp = new Dictionary<string, float>();
            foreach (var p in el.EnumerateObject()) outp[p.Name] = p.Value.GetSingle();
            return outp;
        }

        private static void AssertKeyOrder(IReadOnlyList<string> actualKeys, JsonElement expectedMap, string ctx)
        {
            Assert.That(actualKeys.Count, Is.EqualTo(expectedMap.EnumerateObject().Count()), ctx + " 鍵數");
            var i = 0;
            foreach (var p in expectedMap.EnumerateObject())
            {
                Assert.That(actualKeys[i], Is.EqualTo(p.Name), $"{ctx}[{i}]");
                i++;
            }
        }

        private void AssertRange(NumberRange actual, JsonElement exp, string ctx)
        {
            var items = new List<double>();
            foreach (var v in exp.EnumerateArray()) items.Add(v.GetDouble());
            AssertClose(actual.Min, items[0], ctx + ".min");
            AssertClose(actual.Max, items[1], ctx + ".max");
        }

        private void AssertMap(IReadOnlyDictionary<string, float> actual, JsonElement exp, string ctx)
        {
            var expected = ReadMap(exp);
            Assert.That(actual.Count, Is.EqualTo(expected.Count),
                $"{ctx}：鍵數不符（C#={actual.Count} / Kotlin={expected.Count}）");
            foreach (var kv in expected)
            {
                Assert.That(actual.ContainsKey(kv.Key), Is.True, $"{ctx}：C# 少了鍵 {kv.Key}");
                AssertClose(actual[kv.Key], kv.Value, $"{ctx}.{kv.Key}");
            }
        }

        private void AssertClose(double actual, double expected, string ctx)
        {
            if (Math.Abs(actual - expected) <= _tolerance) return;
            Assert.Fail($"{ctx}：Kotlin={expected} / C#={actual}（Δ={Math.Abs(actual - expected)}）");
        }
    }
}
