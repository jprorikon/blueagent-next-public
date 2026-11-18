using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using BlueAgent.Logic.Engine;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// **fixture 對拍（M4：`VoiceWaitingMotionController`）**：`fixtures/engine_voice_waiting.json`
    /// 由 Kotlin 側 `EngineVoiceWaitingFixtureExportTest` 錄製。
    ///
    /// 這一層的**亂數消耗次序是規格**：`side` → `duration` → `amplitude` → `tempo` → `pickTemplate`
    /// → 模板自己的額外抽（figure-eight／curious-lean／buoyant-bob／contained-tension 各多抽一顆）。
    /// 比的是「開始時的回報值」＋「逐 50ms 的 update」，外加 30 顆種子的**模板分佈**與 reset／逾時分支。
    /// </summary>
    [TestFixture]
    public class EngineVoiceWaitingFixtureTest
    {
        private JsonDocument _fixture;
        private double _tolerance;

        [SetUp]
        public void Load()
        {
            var path = Path.Combine(AppContext.BaseDirectory, "fixtures", "engine_voice_waiting.json");
            Assert.That(File.Exists(path), Is.True, $"找不到 fixture：{path}（先在 Kotlin 側跑 `gradle :engine:test`）");
            _fixture = JsonDocument.Parse(File.ReadAllText(path));
            _tolerance = _fixture.RootElement.GetProperty("tolerance").GetDouble();
        }

        [TearDown]
        public void Unload() => _fixture?.Dispose();

        [Test]
        public void 啟動回報與逐幀取樣相同()
        {
            var cases = _fixture.RootElement.GetProperty("startCases");
            var labels = new HashSet<string>();
            var points = 0;
            var nonEmpty = 0;
            var minKeys = int.MaxValue;

            foreach (var c in cases.EnumerateArray())
            {
                var name = c.GetProperty("name").GetString();
                var timeSeconds = c.GetProperty("timeSeconds").GetDouble();
                var seed = c.GetProperty("seed").GetDouble();
                var controller = new VoiceWaitingMotionController();
                var info = controller.Start(timeSeconds, seed, ReadOptions(c.GetProperty("options")));

                var expInfo = c.GetProperty("info");
                Assert.That(info.Label, Is.EqualTo(expInfo.GetProperty("label").GetString()), name + ".info.label");
                AssertClose(info.Duration, expInfo.GetProperty("duration").GetDouble(), name + ".info.duration");
                AssertClose(info.Amplitude, expInfo.GetProperty("amplitude").GetDouble(), name + ".info.amplitude");
                AssertClose(info.Tempo, expInfo.GetProperty("tempo").GetDouble(), name + ".info.tempo");
                Assert.That(info.Emotion, Is.EqualTo(expInfo.GetProperty("emotion").GetString()), name + ".info.emotion");
                var expVad = expInfo.GetProperty("vad");
                AssertClose(info.Vad.Valence, expVad.GetProperty("valence").GetDouble(), name + ".info.vad.valence");
                AssertClose(info.Vad.Arousal, expVad.GetProperty("arousal").GetDouble(), name + ".info.vad.arousal");
                AssertClose(info.Vad.Dominance, expVad.GetProperty("dominance").GetDouble(), name + ".info.vad.dominance");
                labels.Add(info.Label);

                foreach (var s in c.GetProperty("samples").EnumerateArray())
                {
                    var time = s.GetProperty("time").GetDouble();
                    var gain = s.GetProperty("gain").GetDouble();
                    var actual = controller.Update(time, gain);
                    var exp = s.GetProperty("out");
                    AssertMap(actual, exp, $"{name}@t={time}/gain={gain}");
                    if (actual.Count > 0)
                    {
                        nonEmpty++;
                        minKeys = Math.Min(minKeys, actual.Count);
                    }
                    points++;
                }
            }

            TestContext.Out.WriteLine(
                $"等待動作：{cases.GetArrayLength()} 條 start／{points} 個取樣點（{nonEmpty} 點非空）全數相同；" +
                $"最少鍵數 {minKeys}／共 {labels.Count} 個模板被走到");
            // ⚠️ `update()` 一定回六顆身體／頭部鍵（缺席補 0）⇒ 非空時鍵數不可能少於 6
            Assert.That(minKeys, Is.GreaterThanOrEqualTo(6), "非空的 update 一定包含六顆身體／頭部鍵");
            Assert.That(nonEmpty, Is.GreaterThan(1000), "非空的取樣點太少 ⇒ 時間軸求值沒被真的驗到");
        }

        [Test]
        public void 模板分佈相同()
        {
            var cases = _fixture.RootElement.GetProperty("coverageCases");
            var seen = new HashSet<string>();
            foreach (var c in cases.EnumerateArray())
            {
                var name = c.GetProperty("name").GetString();
                var options = ReadOptions(c.GetProperty("options"));
                var labels = c.GetProperty("labels");
                var i = 0;
                foreach (var expected in labels.EnumerateArray())
                {
                    var controller = new VoiceWaitingMotionController();
                    var actual = controller.Start(0.0, i + 1, options).Label;
                    if (actual != expected.GetString())
                        Assert.Fail($"{name}[seed={i + 1}]：Kotlin={expected.GetString()} / C#={actual}");
                    seen.Add(actual);
                    i++;
                }
            }
            TestContext.Out.WriteLine($"模板分佈：{cases.GetArrayLength()} 組 × 30 顆種子全數相同（共 {seen.Count} 個模板）");
            Assert.That(seen.Count, Is.EqualTo(7), "七個模板不是全部都被走到 ⇒ 有一條沒驗到");
        }

        [Test]
        public void 重置與逾時分支相同()
        {
            var controller = new VoiceWaitingMotionController();
            var steps = _fixture.RootElement.GetProperty("resetCases");
            var outSizes = new List<int>();
            var i = 0;
            foreach (var step in steps.EnumerateArray())
            {
                var kind = step.GetProperty("step").GetString();
                Dictionary<string, float> actual;
                switch (kind)
                {
                    case "start":
                        var info = controller.Start(10.0, 7.0, new VoiceWaitingMotionOptions(emotion: "curious"));
                        Assert.That(info.Label, Is.EqualTo(step.GetProperty("info").GetString()), "start.info");
                        actual = controller.Update(10.5, 1.0);
                        break;
                    case "after_reset":
                        controller.Reset();
                        actual = controller.Update(11.0, 1.0);
                        break;
                    case "start_again":
                        var info2 = controller.Start(12.0, 11.0, new VoiceWaitingMotionOptions(emotion: "sad"));
                        Assert.That(info2.Label, Is.EqualTo(step.GetProperty("info").GetString()), "start_again.info");
                        actual = controller.Update(12.4, 1.0);
                        break;
                    case "past_duration":
                        actual = controller.Update(12.0 + info2Duration(controller) + 0.01, 1.0);
                        break;
                    case "after_expiry":
                        actual = controller.Update(30.0, 1.0);
                        break;
                    default:
                        throw new AssertionException("不認得的 step：" + kind);
                }
                var exp = step.GetProperty("out");
                AssertMap(actual, exp, $"resetCases[{i}].{kind}");
                outSizes.Add(actual.Count);
                i++;
            }
            TestContext.Out.WriteLine("重置／逾時：鍵數序列 = " + string.Join(", ", outSizes));
            Assert.That(outSizes[1], Is.EqualTo(0), "reset() 之後應該是空的");
            Assert.That(outSizes[3], Is.EqualTo(0), "超過 duration 之後應該是空的");
            Assert.That(outSizes[4], Is.EqualTo(0), "逾時後再 update 也應該是空的");
        }

        /// <summary>逾時那一步用：第二段動作的長度（從 fixture 的第三個 step 的取樣推不出來，用同一個 seed 重跑）。</summary>
        private static double info2Duration(VoiceWaitingMotionController controller)
        {
            var probe = new VoiceWaitingMotionController();
            return probe.Start(12.0, 11.0, new VoiceWaitingMotionOptions(emotion: "sad")).Duration;
        }

        private static VoiceWaitingMotionOptions ReadOptions(JsonElement el) => new VoiceWaitingMotionOptions(
            emotion: el.GetProperty("emotion").ValueKind == JsonValueKind.Null ? null : el.GetProperty("emotion").GetString(),
            intensity: el.GetProperty("intensity").ValueKind == JsonValueKind.Null ? (double?)null : el.GetProperty("intensity").GetDouble(),
            vad: el.GetProperty("vad").ValueKind == JsonValueKind.Null ? (VadVector?)null : new VadVector(
                el.GetProperty("vad").GetProperty("valence").GetDouble(),
                el.GetProperty("vad").GetProperty("arousal").GetDouble(),
                el.GetProperty("vad").GetProperty("dominance").GetDouble()));

        private void AssertMap(IReadOnlyDictionary<string, float> actual, JsonElement exp, string ctx)
        {
            Assert.That(actual.Count, Is.EqualTo(exp.EnumerateObject().Count()),
                $"{ctx}：鍵數不符（C#={actual.Count} / Kotlin={exp.EnumerateObject().Count()}）");
            foreach (var p in exp.EnumerateObject())
            {
                Assert.That(actual.ContainsKey(p.Name), Is.True, $"{ctx} 少了 {p.Name}");
                AssertClose(actual[p.Name], p.Value.GetSingle(), $"{ctx}.{p.Name}");
            }
        }

        private void AssertClose(double actual, double expected, string ctx)
        {
            if (actual == expected) return;
            if (double.IsNaN(actual) && double.IsNaN(expected)) return;
            if (Math.Abs(actual - expected) <= _tolerance) return;
            Assert.Fail($"{ctx}：Kotlin={expected} / C#={actual}（Δ={Math.Abs(actual - expected)}）");
        }
    }
}
