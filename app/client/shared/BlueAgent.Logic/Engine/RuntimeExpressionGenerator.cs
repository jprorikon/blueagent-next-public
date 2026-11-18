using System;
using System.Collections.Generic;

namespace BlueAgent.Logic.Engine
{
    /// <summary>
    /// `CharacterPersonality`（角色性格）。
    ///
    /// ⚠️ 別跟 <see cref="EmotionPersonality"/> 搞混：這一顆影響**演出怎麼抽樣**
    /// （expressiveness＝表情幅度、softness＝柔和度、shyness＝害羞傾向），
    /// 那一顆影響**VAD 怎麼跑**（反應速度、衰減率）。預設值是上游的 `defaultPersonality`。
    /// </summary>
    public sealed class CharacterPersonality
    {
        public readonly double Expressiveness;
        public readonly double Softness;
        public readonly double Shyness;
        public readonly double GazeStability;

        public CharacterPersonality(
            double expressiveness = 0.85, double softness = 0.65, double shyness = 0.55, double gazeStability = 0.7)
        {
            Expressiveness = expressiveness;
            Softness = softness;
            Shyness = shyness;
            GazeStability = gazeStability;
        }
    }

    /// <summary>`ExpressionGenerateInput`。</summary>
    public sealed class ExpressionGenerateInput
    {
        public readonly string Emotion;
        public readonly string Variant;
        public readonly double Intensity;
        public readonly IReadOnlyList<string> ContextTags;
        public readonly CharacterPersonality Personality;
        public readonly IReadOnlyDictionary<string, float> PreviousState;
        public readonly double Seed;

        public ExpressionGenerateInput(
            string emotion,
            string variant,
            double intensity,
            IReadOnlyList<string> contextTags = null,
            CharacterPersonality personality = null,
            IReadOnlyDictionary<string, float> previousState = null,
            double seed = 0.0)
        {
            Emotion = emotion;
            Variant = variant;
            Intensity = intensity;
            ContextTags = contextTags ?? new List<string>();
            Personality = personality ?? new CharacterPersonality();
            PreviousState = previousState ?? Facs.CreateDefaultState();
            Seed = seed;
        }
    }

    /// <summary>
    /// 情緒事件 → 演出總譜 —— C# 版，對照 Kotlin `expression/RuntimeExpressionGenerator.kt`。
    ///
    /// 這是「情緒表現」的主幹：把（情緒名 + variant + 強度 + 種子）變成
    ///  * `PeakFACS`：這次情緒最高點的表情，
    ///  * `Timeline`：從「注意力前導 → 表情峰值 → 活體抖動 → 收尾」的關鍵影格串，
    ///  * `IdleBias`：退場後留在待機層的餘韻，
    ///  * `RecoveryDuration`：收尾時間。
    ///
    /// ⚠️ 亂數消耗次序極重要（同一顆 seed 會生出不同演出）：抽樣順序是
    ///    ① variant 挑選 → ② `PeakFACS`（照 ranges 的鍵序）→ ③ headTilt／anticipation／settle／hold
    ///    → ④ livingPeak → ⑤ livingSettle → ⑥ 最後一段的時長 → ⑦ recoveryDuration。
    ///    其中 ④⑤ 只對「base 裡真的有的鍵」抽（順序照 LIVING_JITTER 的宣告序）。
    /// </summary>
    public sealed class RuntimeExpressionGenerator
    {
        /// <summary>轉場時會考慮的鍵（其餘鍵（bodyX/Y/Z 等）不參與轉場）。</summary>
        private static readonly string[] TransitionFacsKeys =
        {
            "browInnerUp", "browOuterUp", "browDown",
            "eyeOpen", "eyeSmile", "eyeSquint", "eyeBlinkL", "eyeBlinkR",
            "mouthSmile", "mouthFrown", "mouthOpen", "mouthPucker",
            "gazeX", "gazeY",
            "blush", "tear", "sweat",
        };

        /// <summary>活體抖動的幅度表 —— 順序照抄（它決定亂數消耗的次序）。</summary>
        private static readonly KeyValuePair<string, double>[] LivingJitter =
        {
            new KeyValuePair<string, double>("browInnerUp", 0.032),
            new KeyValuePair<string, double>("browOuterUp", 0.03),
            new KeyValuePair<string, double>("browDown", 0.026),
            new KeyValuePair<string, double>("eyeOpen", 0.026),
            new KeyValuePair<string, double>("eyeSmile", 0.026),
            new KeyValuePair<string, double>("eyeSquint", 0.022),
            new KeyValuePair<string, double>("mouthSmile", 0.036),
            new KeyValuePair<string, double>("mouthFrown", 0.028),
            new KeyValuePair<string, double>("mouthOpen", 0.032),
            new KeyValuePair<string, double>("mouthPucker", 0.018),
            new KeyValuePair<string, double>("gazeX", 0.036),
            new KeyValuePair<string, double>("gazeY", 0.028),
            new KeyValuePair<string, double>("headX", 0.018),
            new KeyValuePair<string, double>("headY", 0.014),
            new KeyValuePair<string, double>("headZ", 0.024),
            new KeyValuePair<string, double>("blush", 0.026),
            new KeyValuePair<string, double>("tear", 0.018),
            new KeyValuePair<string, double>("sweat", 0.018),
        };

        public RuntimeExpression Generate(ExpressionGenerateInput input)
        {
            var random = EngineMath.SeededRandom(input.Seed);
            var personality = input.Personality;
            var archetype = EmotionArchetypes.Get(input.Emotion);

            string variantName;
            if (input.Variant != null && archetype.Variants.ContainsKey(input.Variant)) variantName = input.Variant;
            else variantName = EngineMath.PickOne(archetype.VariantKeys, random);
            var variant = archetype.Variants[variantName];

            var intensity = (double)EngineMath.Clamp01((float)input.Intensity) * (0.65 + personality.Expressiveness * 0.45);

            // ranges ＝ baseTendency ⊕ variant.ranges（同名鍵由變體覆蓋、**順序保持宣告序**）
            var rangeKeys = new List<string>(archetype.BaseTendencyKeys);
            var ranges = new Dictionary<string, NumberRange>();
            foreach (var k in archetype.BaseTendencyKeys) ranges[k] = archetype.BaseTendency[k];
            foreach (var k in variant.RangeKeys)
            {
                if (!ranges.ContainsKey(k)) rangeKeys.Add(k);
                ranges[k] = variant.Ranges[k];
            }

            var peakFacs = SampleFacs(rangeKeys, ranges, intensity, random);
            ApplyContextBias(peakFacs, input.ContextTags, personality);
            var timeline = BuildTimeline(peakFacs, input.PreviousState, input.Seed, random);

            return new RuntimeExpression(
                emotion: archetype.Emotion,
                variant: variantName,
                intensity: (double)EngineMath.Clamp01((float)input.Intensity),
                timeline: timeline,
                peakFacs: peakFacs,
                idleBias: CreateIdleBias(archetype.Emotion, variantName, peakFacs),
                recoveryDuration: 3.8 + random.Next() * 2.8);
        }

        private static Dictionary<string, float> SampleFacs(
            IReadOnlyList<string> keys,
            IReadOnlyDictionary<string, NumberRange> ranges,
            double intensity,
            RandomSource random)
        {
            var result = new Dictionary<string, float>();
            foreach (var key in keys)
            {
                var sampled = EngineMath.RandomRange(ranges[key], random);
                result[key] = (float)(key == "eyeOpen" ? 1 + (sampled - 1) * intensity : sampled * intensity);
            }
            return Facs.ClampState(result);
        }

        private static void ApplyContextBias(
            Dictionary<string, float> facs, IReadOnlyList<string> contextTags, CharacterPersonality personality)
        {
            if (Contains(contextTags, "compliment"))
            {
                facs["blush"] = Math.Max(Get(facs, "blush", 0f), (float)(0.3 + personality.Shyness * 0.45));
                if (!facs.ContainsKey("gazeX")) facs["gazeX"] = -0.18f;
                if (!facs.ContainsKey("gazeY")) facs["gazeY"] = -0.08f;
            }

            if (Contains(contextTags, "user_tired"))
            {
                facs["browInnerUp"] = Math.Max(Get(facs, "browInnerUp", 0f), (float)(0.22 + personality.Softness * 0.18));
                facs["eyeSmile"] = Math.Max(Get(facs, "eyeSmile", 0f), (float)(0.05 + personality.Softness * 0.1));
            }

            if (Contains(contextTags, "user_good_news"))
            {
                facs["eyeOpen"] = Math.Max(Get(facs, "eyeOpen", 1f), 1.05f);
                facs["mouthSmile"] = Math.Max(Get(facs, "mouthSmile", 0f), 0.55f);
            }
        }

        private static List<RuntimeExpressionKeyframe> BuildTimeline(
            IReadOnlyDictionary<string, float> peakFacs,
            IReadOnlyDictionary<string, float> previousState,
            double seed,
            RandomSource random)
        {
            var headTilt = Get(peakFacs, "headZ", 0f) + (float)((random.Next() - 0.5) * 0.08);
            var hasSmile = Get(peakFacs, "mouthSmile", 0f) > 0.25f;
            var hasConcern = Get(peakFacs, "browInnerUp", 0f) > 0.18f && !hasSmile;
            var anticipationDuration = (float)(0.2 + random.Next() * 0.18);
            var settleDuration = (float)(0.42 + random.Next() * 0.22);
            var holdDuration = (float)(0.5 + random.Next() * 0.42);
            var previousGazeX = Get(previousState, "gazeX", 0f);
            var gazeReturn = Math.Abs(previousGazeX) > 0.16f ? previousGazeX * 0.2f : 0f;

            var attention = new Dictionary<string, float>
            {
                { "gazeX", gazeReturn },
                { "gazeY", 0f },
                { "headX", 0f },
                { "headY", 0f },
                { "eyeOpen", Math.Max(0.96f, Get(peakFacs, "eyeOpen", 1f)) },
                { "browInnerUp", Math.Max(Get(peakFacs, "browInnerUp", 0f), hasConcern ? 0.24f : 0.06f) },
            };

            var faceLead = new Dictionary<string, float>();
            foreach (var k in new[] { "eyeOpen", "browInnerUp", "browOuterUp", "browDown", "gazeX", "gazeY" })
            {
                if (peakFacs.TryGetValue(k, out var v)) faceLead[k] = v;
            }

            var expressionPeak = new Dictionary<string, float>(peakFacs) { ["headZ"] = headTilt };
            var transitionStart = CreateTransitionStart(previousState);
            var transitionAttention = CreateTransitionTarget(previousState, attention);
            var livingPeak = CreateLivingVariant(expressionPeak, random, 1.0);
            var livingSettle = CreateLivingVariant(expressionPeak, random, 0.72);

            var tinyNod = EngineMath.DoubleToIntJava(seed) % 2 == 0 ? -0.03f : 0.035f;

            var timeline = new List<RuntimeExpressionKeyframe>();

            if (transitionStart.Count > 0)
            {
                timeline.Add(new RuntimeExpressionKeyframe(0f, 0f, EasingName.Linear, transitionStart));
            }

            timeline.Add(new RuntimeExpressionKeyframe(0f, anticipationDuration, EasingName.EaseInOut, transitionAttention));
            timeline.Add(new RuntimeExpressionKeyframe(
                anticipationDuration * 0.72f, settleDuration, EasingName.EaseOut,
                Facs.ClampState(new Dictionary<string, float>(faceLead) { ["headY"] = tinyNod })));
            timeline.Add(new RuntimeExpressionKeyframe(
                anticipationDuration + settleDuration * 0.55f, holdDuration * 0.58f, EasingName.EaseInOut,
                Facs.ClampState(expressionPeak)));
            timeline.Add(new RuntimeExpressionKeyframe(
                anticipationDuration + settleDuration * 0.55f + holdDuration * 0.32f, holdDuration * 0.72f,
                EasingName.EaseInOut, livingPeak, weight: 0.96f));
            timeline.Add(new RuntimeExpressionKeyframe(
                // ⚠️ 這裡會抽一顆亂數（最後一段的時長）—— 位置在 livingSettle 之後、recoveryDuration 之前
                anticipationDuration + settleDuration + holdDuration * 0.6f,
                (float)(0.45 + random.Next() * 0.3),
                EasingName.EaseInOut,
                Facs.ClampState(new Dictionary<string, float>(livingSettle)
                {
                    ["mouthOpen"] = hasSmile ? 0.05f : Get(peakFacs, "mouthOpen", 0f),
                    ["headY"] = 0f,
                }),
                weight: 0.82f));

            return timeline;
        }

        /// <summary>從上一幀的狀態裡挑出「已經離開中立值」的鍵 —— 讓轉場從當前表情接續，而不是硬跳。</summary>
        private static Dictionary<string, float> CreateTransitionStart(IReadOnlyDictionary<string, float> previousState)
        {
            var result = new Dictionary<string, float>();
            foreach (var key in TransitionFacsKeys)
            {
                if (!previousState.TryGetValue(key, out var value)) continue;
                if (IsActiveFromNeutral(key, value)) result[key] = value;
            }
            return Facs.ClampState(result);
        }

        private static Dictionary<string, float> CreateTransitionTarget(
            IReadOnlyDictionary<string, float> previousState, IReadOnlyDictionary<string, float> target)
        {
            var result = new Dictionary<string, float>(target);
            var neutral = Facs.CreateDefaultState();
            foreach (var key in TransitionFacsKeys)
            {
                if (result.ContainsKey(key)) continue;
                if (!previousState.TryGetValue(key, out var value)) continue;
                if (IsActiveFromNeutral(key, value)) result[key] = neutral[key];
            }
            return Facs.ClampState(result);
        }

        private static Dictionary<string, float> CreateLivingVariant(
            IReadOnlyDictionary<string, float> baseState, RandomSource random, double amount)
        {
            var neutral = Facs.CreateDefaultState();
            var result = new Dictionary<string, float>(baseState);

            foreach (var kv in LivingJitter)
            {
                if (!baseState.TryGetValue(kv.Key, out var value)) continue;
                if (!neutral.TryGetValue(kv.Key, out var n)) continue;
                var activity = Math.Min(1.0, Math.Abs(value - n) * 2.4 + 0.28);
                result[kv.Key] = (float)(value + (random.Next() - 0.5) * 2 * kv.Value * amount * activity);
            }

            return Facs.ClampState(result);
        }

        private static bool IsActiveFromNeutral(string key, float value)
        {
            if (!Facs.CreateDefaultState().TryGetValue(key, out var neutral)) return false;
            var threshold = key == "eyeOpen" ? 0.012f : 0.018f;
            return Math.Abs(value - neutral) > threshold;
        }

        private static Dictionary<string, float> CreateIdleBias(
            string emotion, string variant, IReadOnlyDictionary<string, float> peakFacs)
        {
            if (emotion == "happy")
            {
                return new Dictionary<string, float>
                {
                    { "mouthSmile", Math.Min(0.16f, Get(peakFacs, "mouthSmile", 0f) * 0.18f) },
                    { "eyeSmile", Math.Min(0.1f, Get(peakFacs, "eyeSmile", 0f) * 0.18f) },
                    { "blush", variant.Contains("shy") ? Math.Min(0.18f, Get(peakFacs, "blush", 0f) * 0.25f) : 0f },
                };
            }

            if (emotion == "concerned")
            {
                return new Dictionary<string, float>
                {
                    { "browInnerUp", Math.Min(0.1f, Get(peakFacs, "browInnerUp", 0f) * 0.2f) },
                    { "mouthSmile", Math.Min(0.08f, Get(peakFacs, "mouthSmile", 0f) * 0.2f) },
                };
            }

            return new Dictionary<string, float> { { "mouthSmile", Math.Min(0.06f, Get(peakFacs, "mouthSmile", 0f) * 0.12f) } };
        }

        private static bool Contains(IReadOnlyList<string> list, string value)
        {
            foreach (var item in list)
            {
                if (item == value) return true;
            }
            return false;
        }

        private static float Get(IReadOnlyDictionary<string, float> map, string key, float fallback) =>
            map.TryGetValue(key, out var v) ? v : fallback;
    }
}
