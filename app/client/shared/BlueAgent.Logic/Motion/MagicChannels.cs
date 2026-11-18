// Port of moeru-ai/airi packages/motion-driver-magic (MIT) — src/shared/channels.ts
// Copyright (c) 2024-PRESENT Neko Ayaka. 授權全文見同資料夾 LICENSE-airi.txt。
using System;
using System.Collections.Generic;

namespace BlueAgent.Logic.Motion
{
    /// <summary>
    /// 一個「會變的」來源欄位，加上所有跟它整段一模一樣的重複欄位（共用一個通道）。
    /// 通道是**無名的數值欄**（按位置），不是骨頭名——幀的每一欄代表什麼由呼叫端決定。
    /// </summary>
    public sealed class MagicChannel
    {
        public List<int> ValueIndices = new List<int>();
        public double Mean;
        public double Scale;
        public double Minimum;
        public double Maximum;
    }

    public static class MagicChannels
    {
        private static double Mean(double[] values)
        {
            var sum = 0.0;
            foreach (var value in values) sum += value;
            return sum / values.Length;
        }

        private static double StandardDeviation(double[] values, double average)
        {
            var sum = 0.0;
            foreach (var value in values) sum += (value - average) * (value - average);
            return Math.Sqrt(sum / values.Length);
        }

        private static double[] Column(IReadOnlyList<double[]> frames, int valueIndex)
        {
            var values = new double[frames.Count];
            for (var i = 0; i < frames.Count; i++) values[i] = frames[i][valueIndex];
            return values;
        }

        private static bool TracksMatch(IReadOnlyList<double[]> frames, int left, int right)
        {
            foreach (var frame in frames)
            {
                if (Math.Abs(frame[left] - frame[right]) > 1e-8) return false;
            }
            return true;
        }

        /// <summary>挑出會變的欄位（標準差 ≤ 1e-8 的常量欄不建模），整段相同的欄位併成一個通道。</summary>
        public static List<MagicChannel> Create(IReadOnlyList<double[]> frames)
        {
            var channels = new List<MagicChannel>();
            for (var valueIndex = 0; valueIndex < frames[0].Length; valueIndex++)
            {
                var values = Column(frames, valueIndex);
                var average = Mean(values);
                var scale = StandardDeviation(values, average);
                if (scale <= 1e-8) continue;

                var matching = channels.Find(channel => TracksMatch(frames, channel.ValueIndices[0], valueIndex));
                if (matching != null)
                {
                    matching.ValueIndices.Add(valueIndex);
                    continue;
                }

                var minimum = double.PositiveInfinity;
                var maximum = double.NegativeInfinity;
                foreach (var value in values)
                {
                    minimum = Math.Min(minimum, value);
                    maximum = Math.Max(maximum, value);
                }
                var channel = new MagicChannel { Mean = average, Scale = scale, Minimum = minimum, Maximum = maximum };
                channel.ValueIndices.Add(valueIndex);
                channels.Add(channel);
            }
            return channels;
        }

        /// <summary>平均幀（含常量欄）：生成時沒建模的欄位就填它。</summary>
        public static double[] Baseline(IReadOnlyList<double[]> frames)
        {
            var baseline = new double[frames[0].Length];
            for (var valueIndex = 0; valueIndex < baseline.Length; valueIndex++)
                baseline[valueIndex] = Mean(Column(frames, valueIndex));
            return baseline;
        }

        /// <summary>標準化的通道值 → 完整的原尺度幀（夾在訓練時的 min／max；重複欄位一起寫）。</summary>
        public static double[] ToFrame(double[] baselineFrame, IReadOnlyList<MagicChannel> channels, double[] values)
        {
            var frame = (double[])baselineFrame.Clone();
            for (var channelIndex = 0; channelIndex < channels.Count; channelIndex++)
            {
                var channel = channels[channelIndex];
                // es-toolkit `clamp(v, min, max)` ＝ `Math.min(Math.max(v, min), max)`
                var raw = Math.Min(Math.Max(channel.Mean + values[channelIndex] * channel.Scale, channel.Minimum),
                                   channel.Maximum);
                foreach (var valueIndex in channel.ValueIndices) frame[valueIndex] = raw;
            }
            return frame;
        }
    }
}
