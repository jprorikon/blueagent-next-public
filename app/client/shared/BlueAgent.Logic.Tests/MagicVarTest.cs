using System;
using System.Collections.Generic;
using BlueAgent.Logic.Motion;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>
    /// MAGIC VAR 的移植對拍（`docs/PLAN-magic-motion-20261031.md` §6.1）。
    /// 真值來自上游 `packages/motion-driver-magic/src/var-ar-hmm.test.ts`（airi，MIT）：同一份合成資料、
    /// 同參數、同 seed，前三幀要對上它寫死的數字。
    /// </summary>
    public class MagicVarTest
    {
        private const int EyeXIndex = 0;
        private const int HeadXIndex = 3;
        private const int HeadYIndex = 4;
        private const int MouthOpenIndex = 10;

        /// <summary>上游 `createTrainingSequence()` 的逐行照抄：240 幀、30 Hz、兩種幅度輪替（13 欄的 Live2D pose）。</summary>
        private static MagicTrainingSequence UpstreamSequence()
        {
            const double sampleRateHz = 30;
            var frames = new List<double[]>();
            for (var frame = 0; frame < 240; frame++)
            {
                var regime = (frame / 60) % 2;
                var phase = (frame % 60) / 60.0 * Math.PI * 2;
                var headX = Math.Sin(phase) * (regime == 0 ? 0.25 : 0.7);
                var headY = Math.Cos(phase * 0.5) * (regime == 0 ? 0.2 : 0.55);
                frames.Add(new[]
                {
                    headX, 0, 0, headX, headY, 0, headX * 0.4, headY * 0.3, 0, 0,
                    (Math.Sin(phase * 2) + 1) * (regime == 0 ? 0.1 : 0.35), 0, 0,
                });
            }
            return new MagicTrainingSequence
            {
                SampleRateHz = sampleRateHz,
                SourceDurationMs = (frames.Count - 1) / sampleRateHz * 1000,
                Frames = frames,
            };
        }

        [Test]
        public void Mulberry32MatchesJavaScriptBitForBit()
        {
            // 值由另一份獨立實作（Python 模擬 JS 的 ToInt32／Math.imul）算出
            var random = new MagicRandom(42);
            Assert.That(random.Next(), Is.EqualTo(0.6011037519201636));
            Assert.That(random.Next(), Is.EqualTo(0.44829055899754167));
            Assert.That(random.Next(), Is.EqualTo(0.8524657934904099));
            Assert.That(random.Next(), Is.EqualTo(0.6697340414393693));
            Assert.That(new MagicRandom(1).Next(), Is.EqualTo(0.6270739405881613));
        }

        [Test]
        public void UpstreamGoldenFramesMatch()
        {
            var model = VarModel.Fit(UpstreamSequence(), 4, 0.001);
            var generator = model.ToGenerator(42);
            var golden = new[]
            {
                new[] { 0.12799432561135884, 0.054573315614377164, 0.024153378051292806 },
                new[] { 0.10509858715154652, 0.03374690235924656, 0.056620804071637526 },
                new[] { 0.08510790150474039, 0.04028754800254499, 0.07558465056506322 },
            };

            Assert.That(model.Diagnostics.SourceFrameCount, Is.EqualTo(240));
            foreach (var expected in golden)
            {
                var values = generator.Next(1);
                // 訓練資料是 sin/cos 產生的：V8 與 .NET 的 sin 末位可能差 1 ulp ⇒ 容差 1e-9（不是逐位）
                Assert.That(values[HeadXIndex], Is.EqualTo(expected[0]).Within(1e-9));
                Assert.That(values[HeadYIndex], Is.EqualTo(expected[1]).Within(1e-9));
                Assert.That(values[MouthOpenIndex], Is.EqualTo(expected[2]).Within(1e-9));
            }
        }

        [Test]
        public void DuplicateTracksFoldAndConstantsAreSkipped()
        {
            var model = VarModel.Fit(UpstreamSequence(), 4, 0.001);
            // 13 欄：會變的是 headX(=eyeX)、headY、bodyX、bodyY、mouthOpen ⇒ 5 個通道
            Assert.That(model.Diagnostics.ChannelCount, Is.EqualTo(5));
            Assert.That(model.Diagnostics.FeatureCount, Is.EqualTo(1 + 4 * 5));
            var generator = model.ToGenerator(42);
            for (var i = 0; i < 8; i++)
            {
                var values = generator.Next(1);
                Assert.That(values[EyeXIndex], Is.EqualTo(values[HeadXIndex]));
                Assert.That(values[1], Is.EqualTo(0));                  // 常量欄 ⇒ 訓練均值
            }
        }

        [Test]
        public void SameSeedSameStreamDifferentSeedDifferentStream()
        {
            var model = VarModel.Fit(UpstreamSequence(), 4, 0.001);
            var left = model.ToGenerator(42);
            var right = model.ToGenerator(42);
            for (var i = 0; i < 8; i++) Assert.That(left.Next(1), Is.EqualTo(right.Next(1)));

            var first = model.ToGenerator(1).Next(1);
            var second = model.ToGenerator(2).Next(1);
            Assert.That(first, Is.Not.EqualTo(second));
        }

        [Test]
        public void GeneratedValuesStayInsideTheTrainingRange()
        {
            var sequence = UpstreamSequence();
            var model = VarModel.Fit(sequence, 4, 0.001);
            var generator = model.ToGenerator(7);
            for (var i = 0; i < 2000; i++)
            {
                var values = generator.Next(3);                         // 噪聲放大 3 倍也不能出界
                Assert.That(values[HeadXIndex], Is.InRange(-0.7, 0.7));
                Assert.That(values[MouthOpenIndex], Is.InRange(0.0, 0.7));
            }
        }

        [Test]
        public void NoiseFreeArSequenceIsRecoveredWithoutRidge()
        {
            // 單位圓上等角速度旋轉：x_t = R · x_{t−1} 精確成立（無噪聲、不衰減）⇒ ridge 0 的擬合殘差應為 0，
            // 生成（noiseScale 0）每一步都是「上一幀轉 0.3 rad，再夾進訓練時的 min／max」。
            // （取樣點不會剛好落在 ±1 ⇒ 極值附近會被夾掉一點，所以不能直接驗半徑＝1）
            const double step = 0.3;
            var frames = new List<double[]>();
            for (var t = 0; t < 200; t++) frames.Add(new[] { Math.Cos(step * t), Math.Sin(step * t) });
            var model = VarModel.Fit(new MagicTrainingSequence { SampleRateHz = 30, Frames = frames }, 1, 0);
            Assert.That(model.Diagnostics.ResidualRootMeanSquare, Is.LessThan(1e-9));

            double[] Bounds(int column)
            {
                double low = double.MaxValue, high = double.MinValue;
                foreach (var frame in frames) { low = Math.Min(low, frame[column]); high = Math.Max(high, frame[column]); }
                return new[] { low, high };
            }
            var xBounds = Bounds(0);
            var yBounds = Bounds(1);
            var generator = model.ToGenerator(3);
            var previous = generator.Next(0);
            for (var i = 0; i < 100; i++)
            {
                var values = generator.Next(0);
                var x = Math.Cos(step) * previous[0] - Math.Sin(step) * previous[1];
                var y = Math.Sin(step) * previous[0] + Math.Cos(step) * previous[1];
                Assert.That(values[0], Is.EqualTo(Math.Min(Math.Max(x, xBounds[0]), xBounds[1])).Within(1e-6));
                Assert.That(values[1], Is.EqualTo(Math.Min(Math.Max(y, yBounds[0]), yBounds[1])).Within(1e-6));
                previous = values;
            }
        }

        [Test]
        public void InvalidInputsThrow()
        {
            var frames = UpstreamSequence().Frames;
            Assert.Throws<ArgumentException>(() =>
                VarModel.Fit(new MagicTrainingSequence { SampleRateHz = 0, Frames = frames }, 4, 0.001));
            Assert.Throws<ArgumentException>(() =>
                VarModel.Fit(new MagicTrainingSequence { SampleRateHz = 30, Frames = frames }, 0, 0.001));
            Assert.Throws<ArgumentException>(() => VarModel.Fit(new MagicTrainingSequence
            {
                SampleRateHz = 30,
                Frames = new List<double[]> { new double[] { 1, 2 }, new double[] { 1, 2 }, new double[] { 1, 2 } },
            }, 1, 0.001));                                                // 全是常量
            Assert.Throws<ArgumentException>(() => VarModel.Fit(new MagicTrainingSequence
            {
                SampleRateHz = 30,
                Frames = new List<double[]> { new double[] { 0 }, new double[] { 1 }, new double[] { 0 } },
            }, 4, 0.001));                                                // 太短
        }

        [Test]
        public void CategoricalAndNormalHelpersBehave()
        {
            var random = new MagicRandom(5);
            var counts = new int[3];
            for (var i = 0; i < 6000; i++) counts[MagicRandom.SampleCategorical(new[] { 0.2, 0.3, 0.5 }, random.Next)]++;
            Assert.That(counts[0] / 6000.0, Is.EqualTo(0.2).Within(0.03));
            Assert.That(counts[2] / 6000.0, Is.EqualTo(0.5).Within(0.03));

            var normal = MagicRandom.Normal(new MagicRandom(9).Next);
            double sum = 0, squares = 0;
            const int n = 20000;
            for (var i = 0; i < n; i++)
            {
                var value = normal();
                sum += value;
                squares += value * value;
            }
            Assert.That(sum / n, Is.EqualTo(0).Within(0.03));
            Assert.That(squares / n, Is.EqualTo(1).Within(0.05));
        }
    }
}
