// Port of moeru-ai/airi packages/motion-driver-magic (MIT) — src/shared/numeric.ts
// Copyright (c) 2024-PRESENT Neko Ayaka. 授權全文見同資料夾 LICENSE-airi.txt。
using System;
using System.Collections.Generic;

namespace BlueAgent.Logic.Motion
{
    /// <summary>
    /// MAGIC 用到的小線性代數。⚠️ 迴圈與加法順序**照抄** TS：浮點對拍（golden 數值）靠的就是同順序。
    /// </summary>
    public static class MagicNumeric
    {
        /// <summary>一列自回歸特徵：`[1, 前 1 幀…, 前 2 幀…, …, 前 order 幀…]`。</summary>
        public static double[] AutoregressiveFeature(IReadOnlyList<double[]> frames, int order, int channelCount,
                                                     int endIndex = -1)
        {
            if (endIndex < 0) endIndex = frames.Count;
            var feature = new double[1 + order * channelCount];
            feature[0] = 1;
            var at = 1;
            for (var lag = 1; lag <= order; lag++)
            {
                var frame = frames[endIndex - lag];
                for (var channel = 0; channel < channelCount; channel++) feature[at++] = frame[channel];
            }
            return feature;
        }

        /// <summary>特徵 × 係數矩陣（`[feature][output]`）。</summary>
        public static double[] Predict(double[][] coefficients, double[] feature)
        {
            var outputCount = coefficients[0].Length;
            var prediction = new double[outputCount];
            for (var featureIndex = 0; featureIndex < feature.Length; featureIndex++)
            {
                for (var outputIndex = 0; outputIndex < outputCount; outputIndex++)
                    prediction[outputIndex] += feature[featureIndex] * coefficients[featureIndex][outputIndex];
            }
            return prediction;
        }

        /// <summary>正定矩陣的下三角 Cholesky 分解；pivot ≤ 1e-12 ⇒ 拋（奇異）。</summary>
        public static double[][] Cholesky(double[][] matrix, string singularMessage)
        {
            var size = matrix.Length;
            var lower = Square(size, size);
            for (var row = 0; row < size; row++)
            {
                for (var column = 0; column <= row; column++)
                {
                    var value = matrix[row][column];
                    for (var index = 0; index < column; index++) value -= lower[row][index] * lower[column][index];
                    if (row == column)
                    {
                        if (value <= 1e-12) throw new InvalidOperationException(singularMessage);
                        lower[row][column] = Math.Sqrt(value);
                    }
                    else
                    {
                        lower[row][column] = value / lower[column][column];
                    }
                }
            }
            return lower;
        }

        /// <summary>解正定方程組 `matrix · X = targets`（多個右手邊一起解）。</summary>
        public static double[][] SolvePositiveDefinite(double[][] matrix, double[][] targets, string singularMessage)
        {
            var lower = Cholesky(matrix, singularMessage);
            var size = matrix.Length;
            var outputCount = targets[0].Length;
            var intermediate = Square(size, outputCount);
            for (var row = 0; row < size; row++)
            {
                for (var output = 0; output < outputCount; output++)
                {
                    var value = targets[row][output];
                    for (var column = 0; column < row; column++) value -= lower[row][column] * intermediate[column][output];
                    intermediate[row][output] = value / lower[row][row];
                }
            }

            var solution = Square(size, outputCount);
            for (var row = size - 1; row >= 0; row--)
            {
                for (var output = 0; output < outputCount; output++)
                {
                    var value = intermediate[row][output];
                    for (var column = row + 1; column < size; column++) value -= lower[column][row] * solution[column][output];
                    solution[row][output] = value / lower[row][row];
                }
            }
            return solution;
        }

        internal static double[][] Square(int rows, int columns)
        {
            var matrix = new double[rows][];
            for (var i = 0; i < rows; i++) matrix[i] = new double[columns];
            return matrix;
        }
    }
}
