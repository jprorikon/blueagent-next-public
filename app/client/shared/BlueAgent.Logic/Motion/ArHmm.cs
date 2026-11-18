// Port of moeru-ai/airi packages/motion-driver-magic (MIT) — src/ar-hmm.ts
// Copyright (c) 2024-PRESENT Neko Ayaka. 授權全文見同資料夾 LICENSE-airi.txt。
using System;
using System.Collections.Generic;

namespace BlueAgent.Logic.Motion
{
    /// <summary>AR-HMM 的擬合參數（airi `FitOptions`）。</summary>
    public sealed class ArHmmOptions
    {
        /// <summary>隱狀態數（動作基元數）；必須 ≥ 2。</summary>
        public int StateCount = 3;
        /// <summary>每個狀態自己的 AR 階數。</summary>
        public int Order = 2;
        /// <summary>ridge 懲罰（相對於每個狀態的有效樣本數）。</summary>
        public double Ridge = 0.01;
        /// <summary>EM 迭代次數。</summary>
        public int Iterations = 4;

        /// <summary>
        /// 轉移矩陣對角線的額外權重（上游是 2）：越大 ⇒ 越不愛換狀態、停留越久。
        /// 待機用 6（換姿勢還是看得到，但不會一直跳）。**預設保持上游 2**，否則對拍會失準。
        /// </summary>
        public double SelfTransitionBias = 2.0;
    }

    /// <summary>AR-HMM 一次擬合的量測值（airi `Diagnostics`）。</summary>
    public sealed class ArHmmDiagnostics
    {
        public int SourceFrameCount;
        public int ChannelCount;
        public int FeatureCount;
        public int StateCount;
        /// <summary>最後一次前向的邊際對數似然 ÷ 幀數。</summary>
        public double MeanLogLikelihoodPerFrame;
        /// <summary>每個隱狀態吃掉的來源幀比例。</summary>
        public double[] StateOccupancy;
        /// <summary>佔用率加權的幾何平均停留時間（秒）。</summary>
        public double MeanDwellSeconds;
    }

    /// <summary>一幀生成結果（airi `Frame`：值＋當時的隱狀態）。</summary>
    public struct ArHmmFrame
    {
        public double[] Values;
        public int State;
    }

    /// <summary>
    /// MAGIC 的 AR-HMM（線性高斯自回歸隱馬可夫）：把動作切成若干「動作基元」，
    /// 每個狀態有自己的 AR 動態與共變異數，用 Baum-Welch（前向-後向 ＋ 最大化）擬合。
    ///
    /// 跟 <see cref="VarModel"/> 的差別：VAR 只有一組動態，AR-HMM 會**在狀態之間跳**，
    /// 所以能表達「換姿勢」這種不連續；代價是擬合重得多（EM 迭代）。
    /// 兩者共用同一種輸出（`Next()` 一幀、`SampleRateHz`），端末可以互換。
    /// </summary>
    public sealed class ArHmmModel
    {
        public string Method => "ar-hmm";
        public double SampleRateHz { get; private set; }
        public ArHmmDiagnostics Diagnostics { get; private set; }
        /// <summary>每一欄的訓練均值（全寬）——待機播放器拿來置中，跟 <see cref="VarModel.Mean"/> 同義。</summary>
        public double[] Mean => Source.Mean;

        internal VarModel Source;
        internal ArHmmOptions Options;
        internal double[] InitialProbabilities;
        internal double[][] TransitionProbabilities;
        internal ArHmmState[] States;
        internal double[][] Posterior;

        /// <summary>擬合（airi `createArHmmModel(sequence, options)`）。</summary>
        public static ArHmmModel Fit(MagicTrainingSequence sequence, ArHmmOptions options)
        {
            if (sequence == null) throw new ArgumentNullException(nameof(sequence));
            options = options ?? new ArHmmOptions();
            if (options.StateCount < 2)
                throw new ArgumentException("The AR-HMM state count must be an integer greater than one.");
            if (options.Iterations < 1)
                throw new ArgumentException("The AR-HMM iteration count must be a positive integer.");

            // 上游先擬一個 VAR 拿到：通道統計、訓練幀、基底幀、來源係數（狀態樣本太少時退回它）
            var source = VarModel.Fit(sequence, options.Order, options.Ridge);
            var rowCount = source.TrainingFrames.Length - options.Order;
            if (rowCount < options.StateCount * (source.Diagnostics.FeatureCount + 1))
                throw new ArgumentException("The current motion is too short for this AR-HMM shape.");

            var clusterFeatures = ClusterFeatures(source.TrainingFrames, options.Order);
            var assignments = InitializeAssignments(clusterFeatures, options.StateCount);
            var expectation = InitialExpectations(assignments, options.StateCount);
            var states = Maximize(source, expectation, options);
            var logLikelihoods = new List<double>();
            for (var iteration = 0; iteration < options.Iterations; iteration++)
            {
                expectation = ExpectationStep(source, states, options);
                logLikelihoods.Add(expectation.LogLikelihood);
                states = Maximize(source, expectation, options);
            }
            expectation = ExpectationStep(source, states, options);
            logLikelihoods.Add(expectation.LogLikelihood);

            var stateWeights = new double[options.StateCount];
            for (var state = 0; state < options.StateCount; state++)
            {
                var total = 0.0;
                foreach (var row in expectation.Gamma) total += row[state];
                stateWeights[state] = total;
            }
            var stateOccupancy = Normalize(stateWeights);
            var meanDwellFrames = 0.0;
            for (var state = 0; state < options.StateCount; state++)
            {
                var leave = Math.Max(1.0 / rowCount, 1.0 - states.TransitionProbabilities[state][state]);
                meanDwellFrames += stateOccupancy[state] / leave;
            }

            var model = new ArHmmModel
            {
                SampleRateHz = source.SampleRateHz,
                Source = source,
                Options = options,
                InitialProbabilities = states.InitialProbabilities,
                TransitionProbabilities = states.TransitionProbabilities,
                States = states.States,
                Posterior = expectation.Gamma,
                Diagnostics = new ArHmmDiagnostics
                {
                    SourceFrameCount = source.Diagnostics.SourceFrameCount,
                    ChannelCount = source.Diagnostics.ChannelCount,
                    FeatureCount = source.Diagnostics.FeatureCount,
                    StateCount = options.StateCount,
                    MeanLogLikelihoodPerFrame = logLikelihoods[logLikelihoods.Count - 1] / expectation.Gamma.Length,
                    StateOccupancy = stateOccupancy,
                    MeanDwellSeconds = meanDwellFrames / source.SampleRateHz,
                },
            };
            return model;
        }

        /// <summary>取生成器（airi `toGenerator({ seed })`）。同一 seed ⇒ 同一串幀。</summary>
        public ArHmmGenerator ToGenerator(uint seed) => new ArHmmGenerator(this, seed);

        // ── 內部：群集特徵與初始化 ──────────────────────────────────────────────
        private static double[][] ClusterFeatures(IReadOnlyList<double[]> frames, int order)
        {
            var channelCount = frames[0].Length;
            var velocityScales = new double[channelCount];
            for (var frameIndex = order; frameIndex < frames.Count; frameIndex++)
            {
                for (var channel = 0; channel < channelCount; channel++)
                {
                    var delta = frames[frameIndex][channel] - frames[frameIndex - 1][channel];
                    velocityScales[channel] += delta * delta;
                }
            }
            for (var channel = 0; channel < channelCount; channel++)
                velocityScales[channel] = Math.Max(1e-6, Math.Sqrt(velocityScales[channel] / (frames.Count - order)));

            var features = new double[frames.Count - order][];
            for (var rowIndex = 0; rowIndex < features.Length; rowIndex++)
            {
                var frame = frames[rowIndex + order];
                var row = new double[channelCount * 2];
                for (var channel = 0; channel < channelCount; channel++)
                {
                    row[channel] = frame[channel];
                    row[channelCount + channel] =
                        (frame[channel] - frames[rowIndex + order - 1][channel]) / velocityScales[channel];
                }
                features[rowIndex] = row;
            }
            return features;
        }

        private static int[] InitializeAssignments(double[][] features, int stateCount)
        {
            var velocityOffset = features[0].Length / 2;
            var quietestIndex = 0;
            var quietestSpeed = double.PositiveInfinity;
            for (var index = 0; index < features.Length; index++)
            {
                var speed = 0.0;
                for (var column = velocityOffset; column < features[index].Length; column++)
                    speed += features[index][column] * features[index][column];
                if (speed < quietestSpeed)
                {
                    quietestSpeed = speed;
                    quietestIndex = index;
                }
            }

            var centroids = new List<double[]> { (double[])features[quietestIndex].Clone() };
            while (centroids.Count < stateCount)
            {
                var farthestIndex = 0;
                var farthestDistance = -1.0;
                for (var index = 0; index < features.Length; index++)
                {
                    var distance = double.PositiveInfinity;
                    foreach (var centroid in centroids)
                        distance = Math.Min(distance, SquaredDistance(features[index], centroid));
                    if (distance > farthestDistance)
                    {
                        farthestDistance = distance;
                        farthestIndex = index;
                    }
                }
                centroids.Add((double[])features[farthestIndex].Clone());
            }

            var assignments = new int[features.Length];
            for (var iteration = 0; iteration < 12; iteration++)
            {
                for (var row = 0; row < features.Length; row++)
                {
                    var bestState = 0;
                    var bestDistance = double.PositiveInfinity;
                    for (var state = 0; state < stateCount; state++)
                    {
                        var distance = SquaredDistance(features[row], centroids[state]);
                        if (distance < bestDistance)
                        {
                            bestDistance = distance;
                            bestState = state;
                        }
                    }
                    assignments[row] = bestState;
                }

                var next = new double[stateCount][];
                var counts = new int[stateCount];
                for (var state = 0; state < stateCount; state++) next[state] = new double[features[0].Length];
                for (var row = 0; row < features.Length; row++)
                {
                    counts[assignments[row]]++;
                    for (var feature = 0; feature < features[row].Length; feature++)
                        next[assignments[row]][feature] += features[row][feature];
                }
                for (var state = 0; state < stateCount; state++)
                {
                    if (counts[state] == 0) continue;
                    for (var feature = 0; feature < next[state].Length; feature++)
                        centroids[state][feature] = next[state][feature] / counts[state];
                }
            }
            return assignments;
        }

        private static double SquaredDistance(double[] left, double[] right)
        {
            var sum = 0.0;
            for (var index = 0; index < left.Length; index++)
            {
                var delta = left[index] - right[index];
                sum += delta * delta;
            }
            return sum;
        }

        private sealed class Expectation
        {
            public double[][] Gamma;
            public double[][] TransitionCounts;
            public double LogLikelihood;
        }

        internal sealed class ArHmmState
        {
            public double[][] Coefficients;
            public double[][] Covariance;
            public double[][] CovarianceCholesky;
        }

        private sealed class ParameterSet
        {
            public double[] InitialProbabilities;
            public double[][] TransitionProbabilities;
            public ArHmmState[] States;
        }

        private static Expectation InitialExpectations(int[] assignments, int stateCount)
        {
            const double floor = 0.001;
            var gamma = new double[assignments.Length][];
            for (var row = 0; row < assignments.Length; row++)
            {
                gamma[row] = new double[stateCount];
                for (var state = 0; state < stateCount; state++)
                    gamma[row][state] = state == assignments[row] ? 1 - floor * (stateCount - 1) : floor;
            }
            var transitionCounts = new double[stateCount][];
            for (var state = 0; state < stateCount; state++)
            {
                transitionCounts[state] = new double[stateCount];
                for (var next = 0; next < stateCount; next++) transitionCounts[state][next] = 0.1;
            }
            for (var row = 1; row < assignments.Length; row++)
                transitionCounts[assignments[row - 1]][assignments[row]]++;
            return new Expectation
            {
                Gamma = gamma,
                TransitionCounts = transitionCounts,
                LogLikelihood = double.NegativeInfinity,
            };
        }

        // ── 內部：最大化 ────────────────────────────────────────────────────────
        private static ParameterSet Maximize(VarModel source, Expectation expectation, ArHmmOptions options)
        {
            var initial = new double[expectation.Gamma[0].Length];
            for (var state = 0; state < initial.Length; state++)
                initial[state] = expectation.Gamma[0][state] + 0.1;

            var transition = new double[options.StateCount][];
            for (var state = 0; state < options.StateCount; state++)
            {
                transition[state] = new double[options.StateCount];
                for (var next = 0; next < options.StateCount; next++)
                    transition[state][next] = expectation.TransitionCounts[state][next]
                                              + (state == next ? options.SelfTransitionBias : 0.1);
                transition[state] = Normalize(transition[state]);
            }

            var states = new ArHmmState[options.StateCount];
            for (var state = 0; state < options.StateCount; state++)
            {
                var coefficients = FitStateCoefficients(source.TrainingFrames, expectation.Gamma, state,
                                                        source, options);
                var covariance = FitStateCovariance(source.TrainingFrames, expectation.Gamma, state,
                                                    coefficients, options);
                states[state] = new ArHmmState
                {
                    Coefficients = coefficients,
                    Covariance = covariance,
                    CovarianceCholesky = MagicNumeric.Cholesky(
                        covariance, "The AR-HMM fit produced a singular covariance."),
                };
            }
            return new ParameterSet
            {
                InitialProbabilities = Normalize(initial),
                TransitionProbabilities = transition,
                States = states,
            };
        }

        private static double[][] FitStateCoefficients(IReadOnlyList<double[]> frames, double[][] gamma,
                                                       int state, VarModel source, ArHmmOptions options)
        {
            var channelCount = source.Diagnostics.ChannelCount;
            var featureCount = 1 + options.Order * channelCount;
            var gram = MagicNumeric.Square(featureCount, featureCount);
            var cross = MagicNumeric.Square(featureCount, channelCount);
            var stateWeight = 0.0;

            for (var row = 0; row < gamma.Length; row++)
            {
                var weight = gamma[row][state];
                var frameIndex = row + options.Order;
                var feature = MagicNumeric.AutoregressiveFeature(frames, options.Order, channelCount, frameIndex);
                var target = frames[frameIndex];
                stateWeight += weight;
                for (var featureRow = 0; featureRow < featureCount; featureRow++)
                {
                    for (var featureColumn = 0; featureColumn <= featureRow; featureColumn++)
                        gram[featureRow][featureColumn] += weight * feature[featureRow] * feature[featureColumn];
                    for (var output = 0; output < channelCount; output++)
                        cross[featureRow][output] += weight * feature[featureRow] * target[output];
                }
            }

            if (stateWeight < featureCount + 1)
            {
                var copy = new double[source.Coefficients.Length][];
                for (var row = 0; row < copy.Length; row++) copy[row] = (double[])source.Coefficients[row].Clone();
                return copy;
            }

            for (var row = 0; row < featureCount; row++)
                for (var column = 0; column < row; column++) gram[column][row] = gram[row][column];
            for (var index = 1; index < featureCount; index++)
                gram[index][index] += options.Ridge * stateWeight;
            return MagicNumeric.SolvePositiveDefinite(
                gram, cross, "The AR-HMM fit produced a singular covariance.");
        }

        private static double[][] FitStateCovariance(IReadOnlyList<double[]> frames, double[][] gamma,
                                                     int state, double[][] coefficients, ArHmmOptions options)
        {
            var channelCount = frames[0].Length;
            var covariance = MagicNumeric.Square(channelCount, channelCount);
            var stateWeight = 0.0;
            for (var row = 0; row < gamma.Length; row++)
            {
                var weight = gamma[row][state];
                var frameIndex = row + options.Order;
                var feature = MagicNumeric.AutoregressiveFeature(frames, options.Order, channelCount, frameIndex);
                var prediction = MagicNumeric.Predict(coefficients, feature);
                stateWeight += weight;
                for (var left = 0; left < channelCount; left++)
                {
                    var residualLeft = frames[frameIndex][left] - prediction[left];
                    for (var right = 0; right <= left; right++)
                    {
                        var residualRight = frames[frameIndex][right] - prediction[right];
                        covariance[left][right] += weight * residualLeft * residualRight;
                    }
                }
            }

            for (var left = 0; left < channelCount; left++)
            {
                for (var right = 0; right <= left; right++)
                {
                    covariance[left][right] /= stateWeight;
                    covariance[right][left] = covariance[left][right];
                }
                covariance[left][left] += 0.0025;      // 上游給的抖動，避免奇異
            }
            return covariance;
        }

        // ── 內部：期望步（前向-後向）────────────────────────────────────────────
        private static double EmissionLogProbability(ArHmmState state, double[] feature, double[] observation)
        {
            var prediction = MagicNumeric.Predict(state.Coefficients, feature);
            var solved = new double[observation.Length];
            for (var row = 0; row < observation.Length; row++)
            {
                var value = observation[row] - prediction[row];
                for (var column = 0; column < row; column++)
                    value -= state.CovarianceCholesky[row][column] * solved[column];
                solved[row] = value / state.CovarianceCholesky[row][row];
            }
            var quadratic = 0.0;
            foreach (var value in solved) quadratic += value * value;
            var logDeterminant = 0.0;
            for (var index = 0; index < state.CovarianceCholesky.Length; index++)
                logDeterminant += Math.Log(state.CovarianceCholesky[index][index]);
            logDeterminant *= 2;
            return -0.5 * (observation.Length * Math.Log(2 * Math.PI) + logDeterminant + quadratic);
        }

        private static Expectation ExpectationStep(VarModel source, ParameterSet parameters, ArHmmOptions options)
        {
            var frames = source.TrainingFrames;
            var rowCount = frames.Length - options.Order;
            var stateCount = options.StateCount;
            var channelCount = source.Diagnostics.ChannelCount;

            var emissions = new double[rowCount][];
            for (var row = 0; row < rowCount; row++)
            {
                var frameIndex = row + options.Order;
                var feature = MagicNumeric.AutoregressiveFeature(frames, options.Order, channelCount, frameIndex);
                emissions[row] = new double[stateCount];
                for (var state = 0; state < stateCount; state++)
                    emissions[row][state] = EmissionLogProbability(parameters.States[state], feature,
                                                                  frames[frameIndex]);
            }

            var logTransitions = new double[stateCount][];
            for (var state = 0; state < stateCount; state++)
            {
                logTransitions[state] = new double[stateCount];
                for (var next = 0; next < stateCount; next++)
                    logTransitions[state][next] = Math.Log(parameters.TransitionProbabilities[state][next]);
            }

            var alpha = new double[rowCount][];
            var firstAlpha = new double[stateCount];
            for (var state = 0; state < stateCount; state++)
                firstAlpha[state] = Math.Log(parameters.InitialProbabilities[state]) + emissions[0][state];
            var scale = LogSumExp(firstAlpha);
            var logLikelihood = scale;
            alpha[0] = new double[stateCount];
            for (var state = 0; state < stateCount; state++) alpha[0][state] = firstAlpha[state] - scale;

            for (var row = 1; row < rowCount; row++)
            {
                alpha[row] = new double[stateCount];
                for (var state = 0; state < stateCount; state++)
                {
                    var terms = new double[stateCount];
                    for (var previous = 0; previous < stateCount; previous++)
                        terms[previous] = alpha[row - 1][previous] + logTransitions[previous][state];
                    alpha[row][state] = emissions[row][state] + LogSumExp(terms);
                }
                scale = LogSumExp(alpha[row]);
                logLikelihood += scale;
                for (var state = 0; state < stateCount; state++) alpha[row][state] -= scale;
            }

            var beta = new double[rowCount][];
            for (var row = 0; row < rowCount; row++) beta[row] = new double[stateCount];
            for (var row = rowCount - 2; row >= 0; row--)
            {
                for (var state = 0; state < stateCount; state++)
                {
                    var terms = new double[stateCount];
                    for (var next = 0; next < stateCount; next++)
                        terms[next] = logTransitions[state][next] + emissions[row + 1][next] + beta[row + 1][next];
                    beta[row][state] = LogSumExp(terms);
                }
                var betaScale = LogSumExp(beta[row]);
                for (var state = 0; state < stateCount; state++) beta[row][state] -= betaScale;
            }

            var gamma = new double[rowCount][];
            for (var row = 0; row < rowCount; row++)
            {
                var values = new double[stateCount];
                for (var state = 0; state < stateCount; state++) values[state] = alpha[row][state] + beta[row][state];
                var normalization = LogSumExp(values);
                gamma[row] = new double[stateCount];
                for (var state = 0; state < stateCount; state++)
                    gamma[row][state] = Math.Exp(values[state] - normalization);
            }

            var transitionCounts = MagicNumeric.Square(stateCount, stateCount);
            for (var row = 0; row < rowCount - 1; row++)
            {
                var values = new double[stateCount * stateCount];
                for (var state = 0; state < stateCount; state++)
                {
                    for (var next = 0; next < stateCount; next++)
                        values[state * stateCount + next] = alpha[row][state] + logTransitions[state][next]
                                                            + emissions[row + 1][next] + beta[row + 1][next];
                }
                var normalization = LogSumExp(values);
                for (var state = 0; state < stateCount; state++)
                    for (var next = 0; next < stateCount; next++)
                        transitionCounts[state][next] +=
                            Math.Exp(values[state * stateCount + next] - normalization);
            }

            return new Expectation
            {
                Gamma = gamma,
                TransitionCounts = transitionCounts,
                LogLikelihood = logLikelihood,
            };
        }

        private static double LogSumExp(IReadOnlyList<double> values)
        {
            var maximum = double.NegativeInfinity;
            foreach (var value in values) maximum = Math.Max(maximum, value);
            var sum = 0.0;
            foreach (var value in values) sum += Math.Exp(value - maximum);
            return maximum + Math.Log(sum);
        }

        private static double[] Normalize(double[] values)
        {
            var sum = 0.0;
            foreach (var value in values) sum += value;
            var result = new double[values.Length];
            for (var index = 0; index < values.Length; index++) result[index] = values[index] / sum;
            return result;
        }
    }

    /// <summary>AR-HMM 生成器：一次 `Next()` 前進一個 <see cref="SampleRateHz"/> 的幀，附當時的隱狀態。</summary>
    public sealed class ArHmmGenerator
    {
        private readonly ArHmmModel _model;
        private readonly MagicRandom _random;
        private readonly Func<double> _normal;
        private readonly List<double[]> _history;
        private int _state;

        public double SampleRateHz => _model.SampleRateHz;
        public int State => _state;

        internal ArHmmGenerator(ArHmmModel model, uint seed)
        {
            _model = model;
            _random = new MagicRandom(seed);
            _normal = MagicRandom.Normal(() => _random.Next());
            var maximumStart = model.Source.TrainingFrames.Length - model.Options.Order;
            var start = (int)Math.Floor(_random.Next() * maximumStart);
            _history = new List<double[]>(model.Options.Order + 1);
            for (var index = start; index < start + model.Options.Order; index++)
                _history.Add((double[])model.Source.TrainingFrames[index].Clone());
            _state = MagicRandom.SampleCategorical(model.Posterior[start], () => _random.Next());
        }

        /// <summary>下一幀（原尺度、全寬）。</summary>
        public ArHmmFrame Next(double noiseScale = 1.0)
        {
            var source = _model.Source;
            var channels = source.Channels;
            _state = MagicRandom.SampleCategorical(_model.TransitionProbabilities[_state], () => _random.Next());
            var state = _model.States[_state];
            var feature = MagicNumeric.AutoregressiveFeature(_history, _model.Options.Order, channels.Count);
            var prediction = MagicNumeric.Predict(state.Coefficients, feature);

            var gaussian = new double[channels.Count];
            for (var index = 0; index < gaussian.Length; index++) gaussian[index] = _normal();

            var nextValues = new double[channels.Count];
            for (var channel = 0; channel < channels.Count; channel++)
            {
                var noise = 0.0;
                for (var sourceIndex = 0; sourceIndex <= channel; sourceIndex++)
                    noise += state.CovarianceCholesky[channel][sourceIndex] * gaussian[sourceIndex];
                var info = channels[channel];
                var raw = info.Mean + (prediction[channel] + noise * noiseScale) * info.Scale;
                var clamped = Math.Min(info.Maximum, Math.Max(info.Minimum, raw));
                nextValues[channel] = (clamped - info.Mean) / info.Scale;
            }

            _history.RemoveAt(0);
            _history.Add(nextValues);
            return new ArHmmFrame
            {
                Values = MagicChannels.ToFrame(source.BaselineFrame, channels, nextValues),
                State = _state,
            };
        }
    }
}
