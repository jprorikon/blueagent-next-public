// Port of moeru-ai/airi packages/motion-driver-magic (MIT) — src/shared/random.ts
// Copyright (c) 2024-PRESENT Neko Ayaka. 授權全文見同資料夾 LICENSE-airi.txt。
using System;
using System.Collections.Generic;

namespace BlueAgent.Logic.Motion
{
    /// <summary>
    /// 可 seed 的均勻亂數（mulberry32），與 airi `createSeededRandom` **逐位相同**。
    ///
    /// JS 那邊 `state` 是不 wrap 的 Number，但每次都經過 `^`／`>>>`／`Math.imul`（ToInt32／ToUint32），
    /// 等於 mod 2^32 ⇒ C# 用 `uint` 的 wrap 加法／乘法就一模一樣；除數同樣是 2^32（double）。
    /// </summary>
    public sealed class MagicRandom
    {
        private uint _state;

        public MagicRandom(uint seed)
        {
            _state = seed;
        }

        /// <summary>[0, 1) 的下一個值。</summary>
        public double Next()
        {
            unchecked
            {
                _state += 0x6D2B79F5u;
                var value = _state;
                value = (value ^ (value >> 15)) * (value | 1u);
                value ^= value + (value ^ (value >> 7)) * (value | 61u);
                return (value ^ (value >> 14)) / 4294967296.0;
            }
        }

        /// <summary>從正規化的類別分佈取一個索引（airi `sampleCategorical`）。</summary>
        public static int SampleCategorical(IReadOnlyList<double> probabilities, Func<double> random)
        {
            var target = random();
            var cumulative = 0.0;
            for (var index = 0; index < probabilities.Count; index++)
            {
                cumulative += probabilities[index];
                if (target <= cumulative) return index;
            }
            return probabilities.Count - 1;
        }

        /// <summary>標準常態亂數流（Box-Muller，一次算兩個、留一個備用；airi `createNormalRandom`）。</summary>
        public static Func<double> Normal(Func<double> random)
        {
            double? spare = null;
            return () =>
            {
                if (spare.HasValue)
                {
                    var value = spare.Value;
                    spare = null;
                    return value;
                }
                // JS `Number.EPSILON` ＝ 2^-52（不是 C# 的 double.Epsilon）
                var radius = Math.Sqrt(-2 * Math.Log(Math.Max(2.220446049250313e-16, random())));
                var angle = 2 * Math.PI * random();
                spare = radius * Math.Sin(angle);
                return radius * Math.Cos(angle);
            };
        }
    }
}
