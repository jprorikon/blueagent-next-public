// Port of moeru-ai/airi packages/motion-driver-magic (MIT) — src/shared/var.ts, src/var.ts, src/types.ts
// Copyright (c) 2024-PRESENT Neko Ayaka. 授權全文見同資料夾 LICENSE-airi.txt。
using System;
using System.Collections.Generic;

namespace BlueAgent.Logic.Motion
{
    /// <summary>一段固定取樣率的動作（airi `TrainingSequence`）：每幀等寬，按時間排。</summary>
    public sealed class MagicTrainingSequence
    {
        /// <summary>`Frames` 的取樣率（幀／秒）。</summary>
        public double SampleRateHz;
        /// <summary>取樣前來源的長度（毫秒；只是紀錄，擬合不用）。</summary>
        public double SourceDurationMs;
        public IReadOnlyList<double[]> Frames;
    }

    /// <summary>一次 VAR 擬合的量測值（airi `Diagnostics`）。</summary>
    public sealed class VarDiagnostics
    {
        /// <summary>用來擬合的來源幀數。</summary>
        public int SourceFrameCount;
        /// <summary>會變、不重複的通道數。</summary>
        public int ChannelCount;
        /// <summary>每條通道方程式的項數（截距＋ order × 通道數）。</summary>
        public int FeatureCount;
        /// <summary>標準化後一步預測殘差的均方根。</summary>
        public double ResidualRootMeanSquare;
    }

    /// <summary>
    /// MAGIC 的 VAR（向量自回歸）模型：`x_t = c + Σ_{k=1..order} A_k · x_{t−k} + ε`，ridge 正規方程擬合。
    ///
    /// 生成**不是**從高斯噪聲起步：seed 先從訓練序列挑一段當開頭，之後每一步＝預測＋**隨機抽一條擬合殘差** × noiseScale，
    /// 再夾在訓練時的 min／max ⇒ 生成的幅度不會超出訓練資料（`docs/PLAN-magic-motion-20261031.md` §2）。
    /// </summary>
    public sealed class VarModel
    {
        private const string SingularMessage = "The VAR fit is numerically singular. Increase the ridge penalty.";

        public int Order { get; private set; }
        public double Ridge { get; private set; }
        /// <summary>生成器一次 `Next()` 前進的是**這個取樣率的一幀**，不是一個渲染幀。</summary>
        public double SampleRateHz { get; private set; }
        public VarDiagnostics Diagnostics { get; private set; }
        /// <summary>每一欄的訓練均值（全寬，含常量欄）——呼叫端拿來置中（本倉加的，上游沒有）。</summary>
        public double[] Mean => (double[])BaselineFrame.Clone();

        internal List<MagicChannel> Channels;
        internal double[][] Coefficients;
        internal double[][] Residuals;
        internal double[][] TrainingFrames;
        internal double[] BaselineFrame;

        /// <summary>擬合（airi `fit(sequence, { method: 'var', order, ridge })`）。</summary>
        public static VarModel Fit(MagicTrainingSequence sequence, int order, double ridge)
        {
            if (sequence == null) throw new ArgumentNullException(nameof(sequence));
            if (double.IsNaN(sequence.SampleRateHz) || double.IsInfinity(sequence.SampleRateHz) || sequence.SampleRateHz <= 0)
                throw new ArgumentException("The motion sample rate must be positive.");
            if (order < 1) throw new ArgumentException("The VAR order must be a positive integer.");

            var frames = sequence.Frames;
            if (frames == null || frames.Count == 0 || frames[0].Length == 0)
                throw new ArgumentException("The motion sequence must contain at least one value.");
            foreach (var frame in frames)
            {
                if (frame.Length != frames[0].Length)
                    throw new ArgumentException("Every motion frame must have the same number of values.");
            }

            var channels = MagicChannels.Create(frames);
            if (channels.Count == 0) throw new ArgumentException("The current motion has no changing channels.");
            if (frames.Count <= order + 1) throw new ArgumentException("The current motion is too short for this VAR order.");

            var baseline = MagicChannels.Baseline(frames);
            var training = new double[frames.Count][];
            for (var f = 0; f < frames.Count; f++)
            {
                training[f] = new double[channels.Count];
                for (var c = 0; c < channels.Count; c++)
                    training[f][c] = (frames[f][channels[c].ValueIndices[0]] - channels[c].Mean) / channels[c].Scale;
            }
            var coefficients = FitCoefficients(training, order, ridge);
            var residuals = CreateResiduals(training, coefficients, order);
            var squaredSum = 0.0;
            foreach (var residual in residuals)
            {
                var channelSum = 0.0;
                foreach (var value in residual) channelSum += value * value;
                squaredSum += channelSum;
            }

            return new VarModel
            {
                Order = order,
                Ridge = ridge,
                SampleRateHz = sequence.SampleRateHz,
                Diagnostics = new VarDiagnostics
                {
                    SourceFrameCount = frames.Count,
                    ChannelCount = channels.Count,
                    FeatureCount = coefficients.Length,
                    ResidualRootMeanSquare = Math.Sqrt(squaredSum / (residuals.Length * channels.Count)),
                },
                Channels = channels,
                Coefficients = coefficients,
                Residuals = residuals,
                TrainingFrames = training,
                BaselineFrame = baseline,
            };
        }

        private static double[][] FitCoefficients(double[][] frames, int order, double ridge)
        {
            var channelCount = frames[0].Length;
            var featureCount = 1 + order * channelCount;
            var gram = MagicNumeric.Square(featureCount, featureCount);
            var cross = MagicNumeric.Square(featureCount, channelCount);

            for (var frameIndex = order; frameIndex < frames.Length; frameIndex++)
            {
                var feature = MagicNumeric.AutoregressiveFeature(frames, order, channelCount, frameIndex);
                var target = frames[frameIndex];
                for (var row = 0; row < featureCount; row++)
                {
                    for (var column = 0; column <= row; column++) gram[row][column] += feature[row] * feature[column];
                    for (var output = 0; output < channelCount; output++) cross[row][output] += feature[row] * target[output];
                }
            }

            for (var row = 0; row < featureCount; row++)
            {
                for (var column = 0; column < row; column++) gram[column][row] = gram[row][column];
            }
            var trainingRowCount = frames.Length - order;
            // 截距（index 0）不罰
            for (var index = 1; index < featureCount; index++) gram[index][index] += ridge * trainingRowCount;

            return MagicNumeric.SolvePositiveDefinite(gram, cross, SingularMessage);
        }

        private static double[][] CreateResiduals(double[][] frames, double[][] coefficients, int order)
        {
            var channelCount = frames[0].Length;
            var residuals = new double[frames.Length - order][];
            for (var frameIndex = order; frameIndex < frames.Length; frameIndex++)
            {
                var feature = MagicNumeric.AutoregressiveFeature(frames, order, channelCount, frameIndex);
                var prediction = MagicNumeric.Predict(coefficients, feature);
                var residual = new double[channelCount];
                for (var channel = 0; channel < channelCount; channel++)
                    residual[channel] = frames[frameIndex][channel] - prediction[channel];
                residuals[frameIndex - order] = residual;
            }
            return residuals;
        }

        /// <summary>一條獨立的生成流（airi `toGenerator({ seed })`）；同模型同 seed ⇒ 同一串幀。</summary>
        public VarGenerator ToGenerator(uint seed) => new VarGenerator(this, seed);
    }

    /// <summary>VAR 生成器：一次 `Next()` 前進一個 <see cref="SampleRateHz"/> 的幀。</summary>
    public sealed class VarGenerator
    {
        private readonly VarModel _model;
        private readonly MagicRandom _random;
        private readonly List<double[]> _history;

        public double SampleRateHz => _model.SampleRateHz;

        internal VarGenerator(VarModel model, uint seed)
        {
            _model = model;
            _random = new MagicRandom(seed);
            var maximumStart = model.TrainingFrames.Length - model.Order;
            var start = (int)Math.Floor(_random.Next() * maximumStart);
            _history = new List<double[]>(model.Order + 1);
            for (var i = start; i < start + model.Order; i++) _history.Add((double[])model.TrainingFrames[i].Clone());
        }

        /// <summary>下一幀（原尺度、全寬：常量欄填訓練均值、重複欄一起寫）。</summary>
        public double[] Next(double noiseScale = 1.0)
        {
            var channels = _model.Channels;
            var feature = MagicNumeric.AutoregressiveFeature(_history, _model.Order, channels.Count);
            var prediction = MagicNumeric.Predict(_model.Coefficients, feature);
            var residual = _model.Residuals[(int)Math.Floor(_random.Next() * _model.Residuals.Length)];
            var nextFrame = new double[channels.Count];
            for (var channelIndex = 0; channelIndex < channels.Count; channelIndex++)
            {
                var channel = channels[channelIndex];
                var raw = channel.Mean + (prediction[channelIndex] + residual[channelIndex] * noiseScale) * channel.Scale;
                var clamped = Math.Min(channel.Maximum, Math.Max(channel.Minimum, raw));
                nextFrame[channelIndex] = (clamped - channel.Mean) / channel.Scale;
            }
            _history.RemoveAt(0);
            _history.Add(nextFrame);
            return MagicChannels.ToFrame(_model.BaselineFrame, channels, nextFrame);
        }
    }
}
