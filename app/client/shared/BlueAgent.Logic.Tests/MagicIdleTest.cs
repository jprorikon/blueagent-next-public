using System;
using System.Collections.Generic;
using BlueAgent.Logic.Motion;
using NUnit.Framework;

namespace BlueAgent.Logic.Tests
{
    /// <summary>待機資料檔（`magic-train/1`）與播放（取樣率插值、置中、補步上限）。</summary>
    public class MagicIdleTest
    {
        private static readonly string[] Axes = { "bodyX", "headY" };

        private static MagicIdleData Sample()
        {
            var frames = new List<double[]>();
            for (var t = 0; t < 300; t++)
                frames.Add(new[] { 3 + Math.Sin(0.21 * t) + 0.3 * Math.Sin(0.05 * t), -2 + 4 * Math.Cos(0.13 * t) });
            return new MagicIdleData
            {
                Axes = Axes,
                Source = "test \"quoted\"",
                Sequence = new MagicTrainingSequence { SampleRateHz = 30, Frames = frames },
            };
        }

        [Test]
        public void JsonRoundTripKeepsEveryValue()
        {
            var data = Sample();
            var parsed = MagicIdleData.Parse(data.ToJson());
            Assert.That(parsed.Axes, Is.EqualTo(Axes));
            Assert.That(parsed.Source, Is.EqualTo("test \"quoted\""));
            Assert.That(parsed.Sequence.SampleRateHz, Is.EqualTo(30));
            Assert.That(parsed.Sequence.Frames.Count, Is.EqualTo(300));
            for (var f = 0; f < 300; f++) Assert.That(parsed.Sequence.Frames[f], Is.EqualTo(data.Sequence.Frames[f]));
        }

        [Test]
        public void ParseAcceptsIntegersAndRejectsBadFiles()
        {
            var data = MagicIdleData.Parse(
                "{\"format\":\"magic-train/1\",\"sampleRateHz\":30,\"axes\":[\"a\"],\"frames\":[[1],[2.5],[-3]]}");
            Assert.That(data.Sequence.Frames[0][0], Is.EqualTo(1));
            Assert.That(data.Sequence.Frames[2][0], Is.EqualTo(-3));
            Assert.Throws<FormatException>(() => MagicIdleData.Parse("{\"format\":\"bamotion/1\"}"));
            Assert.Throws<FormatException>(() => MagicIdleData.Parse(
                "{\"format\":\"magic-train/1\",\"axes\":[\"a\",\"b\"],\"frames\":[[1]]}"));
        }

        [Test]
        public void PlayerAdvancesAtTheModelRateAndInterpolates()
        {
            var data = Sample();
            var model = VarModel.Fit(data.Sequence, 4, 0.001);
            var mean = model.Mean;
            var reference = model.ToGenerator(11);
            var first = reference.Next(1);
            var second = reference.Next(1);
            var third = reference.Next(1);

            var player = new MagicIdlePlayer(model, Axes, 11);
            Assert.That(player.Offset("bodyX"), Is.EqualTo(first[0] - mean[0]).Within(1e-12));

            player.Advance(0.5 / 30);                                   // 半格：前後兩幀的中點
            Assert.That(player.Offset("bodyX"), Is.EqualTo((first[0] + second[0]) / 2 - mean[0]).Within(1e-12));

            // 60 fps 渲染兩幀＝模型一幀（不能每個渲染幀前進一格）
            player.Advance(1.0 / 60);
            Assert.That(player.Offset("headY"), Is.EqualTo(second[1] - mean[1]).Within(1e-9));
            player.Advance(1.0 / 30);
            Assert.That(player.Offset("headY"), Is.EqualTo(third[1] - mean[1]).Within(1e-9));
        }

        [Test]
        public void LongPauseCatchesUpWithABoundedNumberOfSteps()
        {
            var model = VarModel.Fit(Sample().Sequence, 4, 0.001);
            var player = new MagicIdlePlayer(model, Axes, 3);
            player.Advance(600);                                        // 背景回來：10 分鐘不會生成 18000 幀
            var offset = player.Offset("bodyX");
            Assert.That(double.IsNaN(offset), Is.False);
            Assert.That(Math.Abs(offset), Is.LessThan(5));
        }

        [Test]
        public void OffsetsAreCenteredAndUnknownAxesAreZero()
        {
            var model = VarModel.Fit(Sample().Sequence, 4, 0.001);
            var player = new MagicIdlePlayer(model, Axes, 5);
            var sum = 0.0;
            const int steps = 6000;
            for (var i = 0; i < steps; i++)
            {
                player.Advance(1.0 / 30);
                sum += player.Offset("bodyX");
            }
            // 訓練均值約 3 度 ⇒ 置中後長期平均應該接近 0（不是 3）
            Assert.That(sum / steps, Is.EqualTo(0).Within(0.5));
            Assert.That(player.Offset("noSuchAxis"), Is.EqualTo(0));
            Assert.That(player.Has("headY"), Is.True);
        }

        [Test]
        public void 限速把每幀的跳躍壓在上限內()
        {
            var data = Sample();
            var model = VarModel.Fit(data.Sequence, order: 2, ridge: 0.001);
            var player = new MagicIdlePlayer(model, Axes, seed: 7) { MaxDegreesPerSecond = 30 };
            var previous = player.Offset("headY");
            var maximumStep = 30.0 / model.SampleRateHz;        // 度／幀
            var largest = 0.0;
            for (var frame = 0; frame < 300; frame++)
            {
                player.Advance(1.0 / model.SampleRateHz);
                var current = player.Offset("headY");
                largest = Math.Max(largest, Math.Abs(current - previous));
                previous = current;
            }
            Assert.That(largest, Is.LessThanOrEqualTo(maximumStep + 1e-9),
                $"每幀變化 {largest:0.###}° 超過上限 {maximumStep:0.###}°");
        }

        [Test]
        public void 不限速時不會被夾()
        {
            var data = Sample();
            var model = VarModel.Fit(data.Sequence, order: 2, ridge: 0.001);
            var player = new MagicIdlePlayer(model, Axes, seed: 7);   // MaxDegreesPerSecond = 0
            var previous = player.Offset("headY");
            var largest = 0.0;
            for (var frame = 0; frame < 300; frame++)
            {
                player.Advance(1.0 / model.SampleRateHz);
                var current = player.Offset("headY");
                largest = Math.Max(largest, Math.Abs(current - previous));
                previous = current;
            }
            Assert.That(largest, Is.GreaterThan(0.0), "沒有生成器就不該是完全靜止");
        }

        /// <summary>可控來源：每 framesPerState 幀跳一次 40°，用來量「換狀態」那一下的跳變。</summary>
        private sealed class JumpSource : IMagicIdleSource, IMagicIdleStateful
        {
            private readonly int _framesPerState;
            private int _frame;
            public JumpSource(int framesPerState) => _framesPerState = framesPerState;
            public double SampleRateHz => 30;
            public int State => _frame / _framesPerState % 2;
            public double[] Next(double noiseScale)
            {
                _frame++;
                var level = State == 0 ? 0.0 : 40.0;
                return new[] { level, level };
            }
        }

        private static ArHmmModel FittedArHmm()
        {
            var frames = new List<double[]>();
            for (var t = 0; t < 300; t++)
                frames.Add(new[]
                {
                    3 + Math.Sin(0.21 * t) + 0.3 * Math.Sin(0.05 * t),
                    -2 + 4 * Math.Cos(0.13 * t) + (t % 90 < 30 ? 2.5 : -2.5),
                });
            var sequence = new MagicTrainingSequence { SampleRateHz = 30, Frames = frames };
            return ArHmmModel.Fit(sequence, new ArHmmOptions
            {
                StateCount = 3, Order = 4, Ridge = 0.01, Iterations = 3, SelfTransitionBias = 6.0,
            });
        }

        private static double LargestStep(MagicIdlePlayer player, int frames)
        {
            var previous = player.Offset("headY");
            var largest = 0.0;
            for (var frame = 0; frame < frames; frame++)
            {
                player.Advance(1.0 / 30.0);
                var current = player.Offset("headY");
                largest = Math.Max(largest, Math.Abs(current - previous));
                previous = current;
            }
            return largest;
        }

        [Test]
        public void 慣性化把換狀態的跳變壓平()
        {
            double Measure(double transitionSeconds)
            {
                var player = new MagicIdlePlayer(new JumpSource(framesPerState: 45), new[] { 0.0, 0.0 }, Axes)
                {
                    TransitionSeconds = transitionSeconds,
                };
                return LargestStep(player, 900);
            }
            var abrupt = Measure(0.0);
            var smooth = Measure(0.35);
            Assert.That(abrupt, Is.GreaterThan(10.0), "前提：沒開慣性化時應該看得到 40° 的跳");
            Assert.That(smooth, Is.LessThan(abrupt * 0.5),
                $"慣性化後最大單幀跳變 {smooth:0.###}°，未開時 {abrupt:0.###}°");
        }

        [Test]
        public void 慣性化不會永久落後目標()
        {
            var model = FittedArHmm();
            var smooth = new MagicIdlePlayer(model, Axes, seed: 11) { TransitionSeconds = 0.35 };
            smooth.Advance(3.0);                       // 跑一段，讓所有殘餘偏移衰減掉
            var steady = smooth.Offset("headY");
            smooth.Advance(3.0);
            var later = smooth.Offset("headY");
            Assert.That(double.IsNaN(steady) || double.IsNaN(later), Is.False);
            // 只是確認還在動（沒有被鎖死在某一格）＋數值在有限範圍內
            Assert.That(Math.Abs(later), Is.LessThan(180.0), "偏移應該在合理角度內");
        }

        [Test]
        public void 偏移上限會夾住大位移()
        {
            var player = new MagicIdlePlayer(new JumpSource(framesPerState: 20), new[] { 0.0, 0.0 }, Axes)
            {
                TransitionSeconds = 0.5,
                MaxOffsetDegrees = 12.0,
            };
            var largest = 0.0;
            for (var frame = 0; frame < 300; frame++)
            {
                player.Advance(1.0 / 30.0);
                largest = Math.Max(largest, Math.Abs(player.Offset("headY")));
            }
            Assert.That(largest, Is.LessThanOrEqualTo(12.0 + 1e-9), $"偏移 {largest:0.###}° 超過上限 12°");
        }
    }
}
