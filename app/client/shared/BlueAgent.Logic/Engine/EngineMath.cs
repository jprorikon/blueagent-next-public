using System;
using System.Collections.Generic;

namespace BlueAgent.Logic.Engine
{
    /// <summary>`ease` 的曲線名（Kotlin `util/EasingName`）。</summary>
    public enum EasingName
    {
        Linear,
        EaseIn,
        EaseOut,
        EaseInOut,
    }

    /// <summary>區間（Kotlin `util/NumberRange` ＝ `Pair&lt;Double, Double&gt;`；原型表的抽樣範圍）。</summary>
    public readonly struct NumberRange
    {
        public readonly double Min;
        public readonly double Max;

        public NumberRange(double min, double max)
        {
            Min = min;
            Max = max;
        }
    }

    /// <summary>
    /// 亂數來源 —— C# 版，對照 Kotlin `util/seededRandom`（回傳的 closure）。
    ///
    /// ⚠️ 這是**線性同餘**：`v = (v*9301 + 49297) % 233280; return v/233280`，
    ///    而且中間值用 **Double** 存（與 JS／Kotlin 逐位一致）。
    ///    ⇒ 不要換成 `System.Random`、不要改常數：整條情緒鏈的「同一顆 seed ⇒ 同一套演出」靠它。
    /// </summary>
    public sealed class RandomSource
    {
        private double _value;

        public RandomSource(double seed)
        {
            var v = Math.Abs(Math.Floor(seed));
            _value = v == 0.0 ? 1.0 : v;
        }

        /// <summary>抽一顆 0..1（消耗一個值）。</summary>
        public double Next()
        {
            _value = (_value * 9301.0 + 49297.0) % 233280.0;
            return _value / 233280.0;
        }
    }

    /// <summary>
    /// 引擎語意層的數學底 —— C# 版，對照 Kotlin 的 `dev.deepsea.live2d.engine.util`（`Utils.kt`）。
    ///
    /// ⚠️ 這裡刻意**逐條照抄 Kotlin 的寫法**（`clamp` 是 `minOf(max, maxOf(min, value))`），
    /// 不「順手寫得更漂亮」：這些函式是逐幀對拍（1e-4）的對象，改寫法就可能改掉最後幾個位元。
    /// 尤其是 <see cref="SeededRandom"/>／<see cref="DeriveMotionSeed"/> 的 32 位元雜湊——
    /// 那兩支的中間值必須與 JS 逐位一致（JS 用 double 存 LCG 的乘積、用 int32 做雜湊）。
    /// </summary>
    public static class EngineMath
    {
        /// <summary>`clamp(value, min, max)`：Kotlin 是 `minOf(max, maxOf(min, value))`。</summary>
        public static float Clamp(float value, float min, float max) => Math.Min(max, Math.Max(min, value));

        /// <summary>`clamp` 的 Double 版。</summary>
        public static double Clamp(double value, double min, double max) => Math.Min(max, Math.Max(min, value));

        /// <summary>`clamp01`。</summary>
        public static float Clamp01(float value) => Clamp(value, 0f, 1f);

        /// <summary>`clamp01` 的 Double 版。</summary>
        public static double Clamp01(double value) => Clamp(value, 0.0, 1.0);

        /// <summary>`lerp(from, to, t)`。</summary>
        public static float Lerp(float from, float to, float t) => from + (to - from) * t;

        /// <summary>`lerp` 的 Double 版。</summary>
        public static double Lerp(double from, double to, double t) => from + (to - from) * t;

        /// <summary>`ease(name, t)`（Float 版；`ExpressionTimeline` 走這條）。</summary>
        public static float Ease(EasingName name, float t)
        {
            var x = Clamp01(t);
            switch (name)
            {
                case EasingName.EaseIn: return x * x;
                case EasingName.EaseOut: return 1f - (1f - x) * (1f - x);
                case EasingName.EaseInOut:
                    return x < 0.5f ? 2f * x * x : 1f - (-2f * x + 2f) * (-2f * x + 2f) / 2f;
                default: return x;
            }
        }

        /// <summary>
        /// `ease` 的 **Double** 版：參考實作全程用 double，遇到「求值要跟 JS 逐位對齊」的場合
        /// （例如表演曲線取樣）用這支，不要用 Float 版再轉 —— Float 會在 1e-8 級留下差。
        /// </summary>
        public static double Ease(EasingName name, double t)
        {
            var x = Clamp01(t);
            switch (name)
            {
                case EasingName.EaseIn: return x * x;
                case EasingName.EaseOut: return 1.0 - (1.0 - x) * (1.0 - x);
                case EasingName.EaseInOut:
                    return x < 0.5 ? 2.0 * x * x : 1.0 - (-2.0 * x + 2.0) * (-2.0 * x + 2.0) / 2.0;
                default: return x;
            }
        }

        /// <summary>`smoothDamp(current, target, factor)`。</summary>
        public static float SmoothDamp(float current, float target, float factor) =>
            current + (target - current) * factor;

        /// <summary>
        /// `smoothingFactor(speed, deltaSeconds)`：profile 的 `parameterSmoothing` 是**速度**
        /// （speed）、不是時間常數 —— 預設 14。
        /// </summary>
        public static float SmoothingFactor(float speed, float deltaSeconds) =>
            1f - (float)Math.Exp(-Math.Max(0f, speed) * Math.Max(0f, deltaSeconds));

        /// <summary>`tau()`：0..1 的相位換成弧度（控制器裡到處在用）。</summary>
        public static float Tau() => (float)(2.0 * Math.PI);

        /// <summary>
        /// `Math.round(double)`（**Java 語意**：`floor(x + 0.5)`）。
        /// ⚠️ 不要用 C# 的 `Math.Round`：那是 banker's rounding（2.5 → 2），Java 是 3
        ///    ——`avoidRepeatWindow`、手勢的 `repeatWindow`、推導種子都走這條，差一就整條歪。
        /// </summary>
        public static double RoundJava(double value) => Math.Floor(value + 0.5);

        /// <summary>
        /// `Double.toLong()`（**Java 語意**：往零截斷、超範圍**飽和**、NaN ⇒ 0）。
        /// ⚠️ C# 的 `(long)d` 對超範圍是未定義 ⇒ 別直接寫 `(long)value`。
        /// </summary>
        public static long DoubleToLongJava(double value)
        {
            if (double.IsNaN(value)) return 0L;
            if (value >= long.MaxValue) return long.MaxValue;
            if (value <= long.MinValue) return long.MinValue;
            return (long)value;
        }

        /// <summary>`Math.round(x)`（Java 語意：`floor(x + 0.5)`）——等待動作的預設種子用。</summary>
        public static long RoundJavaToLong(double value) => (long)Math.Floor(value + 0.5);

        /// <summary>`Math.round(...).toInt()`（Java 語意）。</summary>
        public static int RoundJavaToInt(double value) => (int)Math.Floor(value + 0.5);

        /// <summary>
        /// `Double.toInt()`（**Java 語意**：往零截斷、超範圍**飽和**成 Int.MAX／MIN、NaN ⇒ 0）。
        /// ⚠️ C# 的 `(int)d` 對超範圍是未定義 ⇒ 別直接寫 `(int)value`。
        /// </summary>
        public static int DoubleToIntJava(double value)
        {
            if (double.IsNaN(value)) return 0;
            if (value >= int.MaxValue) return int.MaxValue;
            if (value <= int.MinValue) return int.MinValue;
            return (int)value;
        }

        /// <summary>`seededRandom(seed)`：線性同餘（見 <see cref="RandomSource"/>）。</summary>
        public static RandomSource SeededRandom(double seed) => new RandomSource(seed);

        /// <summary>`Math.imul`：兩個 32 位有號整數相乘，只留低 32 位。</summary>
        public static ulong Imul32(ulong a, ulong b) => ((a & 0xFFFFFFFFUL) * (b & 0xFFFFFFFFUL)) & 0xFFFFFFFFUL;

        /// <summary>
        /// `deriveMotionSeed(seed, channel)`：把一個種子散成子種子
        /// （xorshift-multiply，逐位對齊 JS 的 int32 運算）。
        ///
        /// ⚠️ 兩個坑（Kotlin 檔頭寫的，照抄）：
        ///   ① 全部用「無號 32 位」做（JS 是 `Math.imul(...) >>> 0`）——用有號數在乘積溢位的邊界會寫出看不出來的錯；
        ///   ② 最後一步的 `normalizeSeed` 會 `abs()`，而 JS 那時的 `value` 是**有號 int32**
        ///      ⇒ 最高位是 1 時要先把符號翻正再 abs（否則只有「某些通道」錯，症狀是眨眼時間整組跑掉）。
        /// </summary>
        public static double DeriveMotionSeed(double seed, int channel)
        {
            var v = (NormalizeSeedToUint(seed) ^ Imul32((ulong)(channel + 1), 0x9e3779b1UL)) & 0xFFFFFFFFUL;
            v = (v ^ (v >> 16)) & 0xFFFFFFFFUL;
            v = Imul32(v, 0x85ebca6bUL);
            v = (v ^ (v >> 13)) & 0xFFFFFFFFUL;
            v = Imul32(v, 0xc2b2ae35UL);
            v = (v ^ (v >> 16)) & 0xFFFFFFFFUL;

            var signed = (v & 0x80000000UL) != 0UL ? (double)((long)v - 0x100000000L) : (double)v;
            return NormalizeSeedToUintValue(signed);
        }

        /// <summary>`normalizeSeed`：非有限 ⇒ 1；否則取 uint32、0 ⇒ 1。</summary>
        public static double NormalizeSeed(double seed) => NormalizeSeedToUintValue(seed);

        private static ulong NormalizeSeedToUint(double seed)
        {
            if (double.IsNaN(seed) || double.IsInfinity(seed)) return 1UL;
            var v = (ulong)Math.Abs(Math.Floor(seed)) & 0xFFFFFFFFUL;
            return v == 0UL ? 1UL : v;
        }

        private static double NormalizeSeedToUintValue(double seed)
        {
            if (double.IsNaN(seed) || double.IsInfinity(seed)) return 1.0;
            var v = (ulong)Math.Abs(Math.Floor(seed)) & 0xFFFFFFFFUL;
            return v == 0UL ? 1.0 : (double)v;
        }

        /// <summary>`randomRange(range, random)`：範圍取樣（原型表填「範圍」，抽樣時才定案）。</summary>
        public static double RandomRange(NumberRange range, RandomSource random) =>
            range.Min + (range.Max - range.Min) * random.Next();

        /// <summary>
        /// `pickOne(items, random)`：⚠️ 參考引擎是 `Math.floor(random()*n) % n`，
        /// **取模不能省**（浮點邊界會取到 n）。
        /// </summary>
        public static T PickOne<T>(IReadOnlyList<T> items, RandomSource random) =>
            items[(int)Math.Floor(random.Next() * items.Count) % items.Count];
    }
}
