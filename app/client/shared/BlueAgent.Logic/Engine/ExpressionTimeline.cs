using System.Collections.Generic;

namespace BlueAgent.Logic.Engine
{
    /// <summary>`RuntimeExpressionKeyframe`：時間軸上的一個關鍵影格。</summary>
    public sealed class RuntimeExpressionKeyframe
    {
        public readonly float Time;
        public readonly float Duration;
        public readonly EasingName Easing;
        public readonly IReadOnlyDictionary<string, float> Facs;
        public readonly float Weight;

        public RuntimeExpressionKeyframe(
            float time, float duration, EasingName easing, IReadOnlyDictionary<string, float> facs, float weight = 1f)
        {
            Time = time;
            Duration = duration;
            Easing = easing;
            Facs = facs;
            Weight = weight;
        }
    }

    /// <summary>`RuntimeExpression`：一次情緒事件的「演出總譜」。</summary>
    public sealed class RuntimeExpression
    {
        public readonly string Emotion;
        public readonly string Variant;
        public readonly double Intensity;
        public readonly IReadOnlyList<RuntimeExpressionKeyframe> Timeline;
        public readonly IReadOnlyDictionary<string, float> PeakFACS;
        public readonly IReadOnlyDictionary<string, float> IdleBias;
        public readonly double RecoveryDuration;

        public RuntimeExpression(
            string emotion, string variant, double intensity,
            IReadOnlyList<RuntimeExpressionKeyframe> timeline,
            IReadOnlyDictionary<string, float> peakFacs,
            IReadOnlyDictionary<string, float> idleBias,
            double recoveryDuration)
        {
            Emotion = emotion;
            Variant = variant;
            Intensity = intensity;
            Timeline = timeline;
            PeakFACS = peakFacs;
            IdleBias = idleBias;
            RecoveryDuration = recoveryDuration;
        }
    }

    /// <summary>
    /// 把「表達時間軸」求值成當下的表情 —— C# 版，對照 Kotlin `expression/ExpressionTimeline.kt`。
    ///
    /// 時間軸是一串關鍵影格（每個有 `Time`＝起點、`Duration`＝過渡長度、`Easing`、`Weight`）。
    /// 求值規則：**把所有已經開始的影格依序疊上去**（不是只取最後一個）—— 所以時間軸的順序
    /// 與 weight 都會影響結果。
    /// </summary>
    public static class ExpressionTimeline
    {
        /// <summary>`getTimelineDuration`：最後一個影格的結束時間。</summary>
        public static float Duration(IReadOnlyList<RuntimeExpressionKeyframe> timeline)
        {
            var max = 0f;
            foreach (var frame in timeline) max = System.Math.Max(max, frame.Time + frame.Duration);
            return max;
        }

        public static Dictionary<string, float> Evaluate(
            IReadOnlyList<RuntimeExpressionKeyframe> timeline, float elapsedSeconds)
        {
            var result = new Dictionary<string, float>();

            foreach (var frame in timeline)
            {
                if (elapsedSeconds < frame.Time) continue;

                var local = frame.Duration <= 0f ? 1f : (elapsedSeconds - frame.Time) / frame.Duration;
                var eased = EngineMath.Ease(frame.Easing, System.Math.Min(1f, local));
                result = BlendFrame(result, frame.Facs, eased, frame.Weight);
            }

            return result;
        }

        private static Dictionary<string, float> BlendFrame(
            IReadOnlyDictionary<string, float> current,
            IReadOnlyDictionary<string, float> target,
            float progress,
            float weight)
        {
            var neutral = Facs.CreateDefaultState();
            var result = new Dictionary<string, float>(current);

            foreach (var kv in target)
            {
                if (!neutral.TryGetValue(kv.Key, out var neutralValue)) continue;
                var weightedTarget = EngineMath.Lerp(neutralValue, kv.Value, weight);
                // ⚠️ 讀的是**原本的** current（不是一路疊上去的 result）——照 Kotlin 寫，別改成 result
                var fromValue = current.TryGetValue(kv.Key, out var cv) ? cv : neutralValue;
                result[kv.Key] = EngineMath.Lerp(fromValue, weightedTarget, progress);
            }

            return Facs.ClampState(result);
        }
    }
}
