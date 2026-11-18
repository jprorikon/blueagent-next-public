using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using BlueAgent.Logic.Motion;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// MAGIC AR-HMM 的移植對拍（`docs/PLAN-magic-motion-20261031.md` §6.4）。
    ///
    /// 真值不是我們自己算的：`fixtures/arhmm-golden.json` 是**直接用上游 airi（MIT）的 TypeScript
    /// 實作**跑出來的（`packages/motion-driver-magic/src/ar-hmm.ts`，產生器見 commit 說明），
    /// 裡面有同一份合成序列、同參數、同 seed 的擬合診斷與 40 幀生成結果。
    /// 判據：診斷值誤差 &lt; 1e-9、每一幀的每個值與隱狀態**完全一致**。
    /// </summary>
    public class ArHmmTest
    {
        private const int Frames = 240;
        private const int Channels = 3;
        private const int Order = 2;
        private const int StateCount = 3;
        private const double Ridge = 0.01;
        private const int Iterations = 4;
        private const uint Seed = 12345;

        private JsonDocument _fixture;

        [SetUp]
        public void Load()
        {
            var path = Path.Combine(AppContext.BaseDirectory, "fixtures", "arhmm-golden.json");
            Assert.That(File.Exists(path), Is.True, $"找不到 fixture：{path}（用 airi-harness/harness.ts 重產）");
            _fixture = JsonDocument.Parse(File.ReadAllText(path));
        }

        [TearDown]
        public void Unload() => _fixture?.Dispose();

        /// <summary>與 harness.ts 同一條公式（不用亂數，兩邊才對得上）。</summary>
        private static MagicTrainingSequence Sequence()
        {
            var frames = new List<double[]>(Frames);
            for (var i = 0; i < Frames; i++)
            {
                var t = i / 30.0;
                frames.Add(new[]
                {
                    Math.Sin(t * 1.1) * 0.8 + Math.Sin(t * 0.37) * 0.3,
                    Math.Cos(t * 0.9) * 0.6 + Math.Sin(t * 0.21) * 0.2,
                    Math.Sin(t * 2.3 + 1.0) * 0.25,
                });
            }
            return new MagicTrainingSequence
            {
                SampleRateHz = 30,
                SourceDurationMs = Frames / 30.0 * 1000,
                Frames = frames,
            };
        }

        private static ArHmmModel Fit() => ArHmmModel.Fit(Sequence(), new ArHmmOptions
        {
            StateCount = StateCount,
            Order = Order,
            Ridge = Ridge,
            Iterations = Iterations,
        });

        [Test]
        public void 診斷值與上游相同()
        {
            var expected = _fixture.RootElement.GetProperty("diagnostics");
            var model = Fit();
            var actual = model.Diagnostics;

            Assert.That(actual.SourceFrameCount, Is.EqualTo(expected.GetProperty("sourceFrameCount").GetInt32()));
            Assert.That(actual.ChannelCount, Is.EqualTo(expected.GetProperty("channelCount").GetInt32()));
            Assert.That(actual.FeatureCount, Is.EqualTo(expected.GetProperty("featureCount").GetInt32()));
            Assert.That(actual.StateCount, Is.EqualTo(expected.GetProperty("stateCount").GetInt32()));
            Assert.That(actual.MeanLogLikelihoodPerFrame,
                Is.EqualTo(expected.GetProperty("meanLogLikelihoodPerFrame").GetDouble()).Within(1e-9),
                "平均對數似然：EM 有做對的話這個值會一模一樣");
            Assert.That(actual.MeanDwellSeconds,
                Is.EqualTo(expected.GetProperty("meanDwellSeconds").GetDouble()).Within(1e-9));

            var occupancy = expected.GetProperty("stateOccupancy");
            Assert.That(actual.StateOccupancy.Length, Is.EqualTo(occupancy.GetArrayLength()));
            for (var state = 0; state < occupancy.GetArrayLength(); state++)
                Assert.That(actual.StateOccupancy[state], Is.EqualTo(occupancy[state].GetDouble()).Within(1e-9),
                    $"狀態 {state} 的佔用率");
        }

        [Test]
        public void 逐幀生成與上游相同()
        {
            var expected = _fixture.RootElement.GetProperty("generated");
            var generator = Fit().ToGenerator(Seed);
            var count = expected.GetArrayLength();
            Assert.That(count, Is.GreaterThan(20), "fixture 太短，對拍沒有意義");

            for (var frame = 0; frame < count; frame++)
            {
                var item = expected[frame];
                var actual = generator.Next();
                Assert.That(actual.State, Is.EqualTo(item.GetProperty("state").GetInt32()),
                    $"第 {frame} 幀的隱狀態（狀態抽樣順序不一致就是 PRNG 或轉移矩陣有問題）");
                var values = item.GetProperty("values");
                Assert.That(actual.Values.Length, Is.EqualTo(values.GetArrayLength()));
                for (var channel = 0; channel < values.GetArrayLength(); channel++)
                    Assert.That(actual.Values[channel],
                        Is.EqualTo(values[channel].GetDouble()).Within(1e-9),
                        $"第 {frame} 幀第 {channel} 欄");
            }
        }

        [Test]
        public void 狀態數太小的序列會被擋下()
        {
            var shortSequence = new MagicTrainingSequence
            {
                SampleRateHz = 30,
                SourceDurationMs = 1000,
                Frames = new List<double[]>
                {
                    new[] { 0.0, 0.0, 0.0 }, new[] { 0.1, 0.1, 0.1 }, new[] { 0.2, 0.0, 0.2 },
                    new[] { 0.3, 0.1, 0.1 }, new[] { 0.4, 0.0, 0.0 },
                },
            };
            Assert.Throws<ArgumentException>(() => ArHmmModel.Fit(shortSequence, new ArHmmOptions
            {
                StateCount = 3, Order = 2, Ridge = 0.01, Iterations = 2,
            }));
        }
    }
}
