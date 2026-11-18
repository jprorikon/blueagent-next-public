using System.Collections.Generic;

namespace BlueAgent.Logic.Engine
{
    /// <summary>
    /// 情緒原型表 —— **產生物，不要手改**（`tools/gen_emotion_archetypes_cs.py` 從 Kotlin 的
    /// `expression/EmotionArchetypeRegistry.kt` 翻過來；那份本身也是上游產生的）。
    ///
    /// ⚠️ **順序是資料的一部分**：`RuntimeExpressionGenerator.sampleFacs` 照 `BaseTendencyKeys` 的順序
    ///    抽亂數、`pickOne` 照 `VariantKeys` 的順序挑變體 ⇒ 順序換掉＝同一顆 seed 生出不同演出。
    ///    所以每一層都同時帶「有序鍵清單」與「查表」。
    /// </summary>
    public sealed class EmotionVariant
    {
        public readonly IReadOnlyList<string> RangeKeys;
        public readonly IReadOnlyDictionary<string, NumberRange> Ranges;
        public readonly IReadOnlyList<string> Tags;

        public EmotionVariant(
            IReadOnlyList<string> rangeKeys,
            IReadOnlyDictionary<string, NumberRange> ranges,
            IReadOnlyList<string> tags)
        {
            RangeKeys = rangeKeys;
            Ranges = ranges;
            Tags = tags;
        }
    }

    /// <summary>一個情緒的原型：基礎傾向 ＋ 若干變體。</summary>
    public sealed class EmotionArchetype
    {
        public readonly string Emotion;
        public readonly IReadOnlyList<string> BaseTendencyKeys;
        public readonly IReadOnlyDictionary<string, NumberRange> BaseTendency;
        public readonly IReadOnlyList<string> VariantKeys;
        public readonly IReadOnlyDictionary<string, EmotionVariant> Variants;

        public EmotionArchetype(
            string emotion,
            IReadOnlyList<string> baseTendencyKeys,
            IReadOnlyDictionary<string, NumberRange> baseTendency,
            IReadOnlyList<string> variantKeys,
            IReadOnlyDictionary<string, EmotionVariant> variants)
        {
            Emotion = emotion;
            BaseTendencyKeys = baseTendencyKeys;
            BaseTendency = baseTendency;
            VariantKeys = variantKeys;
            Variants = variants;
        }
    }

    public static class EmotionArchetypes
    {
        /// <summary>照上游的宣告順序（順序會影響 variant 挑選時的平手取捨）。</summary>
        public static readonly IReadOnlyList<KeyValuePair<string, EmotionArchetype>> List = new[]
        {
            new KeyValuePair<string, EmotionArchetype>("neutral", new EmotionArchetype(
                "neutral",
                new string[] { "mouthSmile", "eyeOpen", "eyeSmile", "browInnerUp", "gazeX", "gazeY", "headZ" },
                new Dictionary<string, NumberRange> { { "mouthSmile", new NumberRange(0.02, 0.12) }, { "eyeOpen", new NumberRange(0.9, 1.0) }, { "eyeSmile", new NumberRange(0.0, 0.12) }, { "browInnerUp", new NumberRange(0.0, 0.06) }, { "gazeX", new NumberRange(-0.06, 0.06) }, { "gazeY", new NumberRange(-0.03, 0.04) }, { "headZ", new NumberRange(-0.03, 0.03) } },
                new string[] { "neutral_ack", "attentive" },
                new Dictionary<string, EmotionVariant>
                {
                    { "neutral_ack", new EmotionVariant(
                        new string[] { "mouthSmile", "eyeSmile", "headY" },
                        new Dictionary<string, NumberRange> { { "mouthSmile", new NumberRange(0.04, 0.16) }, { "eyeSmile", new NumberRange(0.0, 0.1) }, { "headY", new NumberRange(-0.03, 0.03) } },
                        new string[] {  }) },
                    { "attentive", new EmotionVariant(
                        new string[] { "eyeOpen", "browInnerUp", "gazeX", "gazeY" },
                        new Dictionary<string, NumberRange> { { "eyeOpen", new NumberRange(0.96, 1.06) }, { "browInnerUp", new NumberRange(0.04, 0.12) }, { "gazeX", new NumberRange(-0.02, 0.02) }, { "gazeY", new NumberRange(-0.02, 0.02) } },
                        new string[] {  }) },
                })),
            new KeyValuePair<string, EmotionArchetype>("happy", new EmotionArchetype(
                "happy",
                new string[] { "mouthSmile", "eyeSmile", "eyeOpen", "browInnerUp", "headZ", "gazeX", "gazeY" },
                new Dictionary<string, NumberRange> { { "mouthSmile", new NumberRange(0.35, 0.85) }, { "eyeSmile", new NumberRange(0.15, 0.55) }, { "eyeOpen", new NumberRange(0.88, 1.08) }, { "browInnerUp", new NumberRange(0.0, 0.18) }, { "headZ", new NumberRange(-0.08, 0.08) }, { "gazeX", new NumberRange(-0.12, 0.12) }, { "gazeY", new NumberRange(-0.04, 0.08) } },
                new string[] { "soft_smile", "bright_smile", "surprised_happy", "shy_happy" },
                new Dictionary<string, EmotionVariant>
                {
                    { "soft_smile", new EmotionVariant(
                        new string[] { "mouthSmile", "eyeSmile", "headZ" },
                        new Dictionary<string, NumberRange> { { "mouthSmile", new NumberRange(0.3, 0.55) }, { "eyeSmile", new NumberRange(0.1, 0.3) }, { "headZ", new NumberRange(-0.04, 0.04) } },
                        new string[] {  }) },
                    { "bright_smile", new EmotionVariant(
                        new string[] { "mouthSmile", "eyeSmile", "browInnerUp", "headY" },
                        new Dictionary<string, NumberRange> { { "mouthSmile", new NumberRange(0.6, 0.9) }, { "eyeSmile", new NumberRange(0.35, 0.65) }, { "browInnerUp", new NumberRange(0.05, 0.18) }, { "headY", new NumberRange(-0.03, 0.06) } },
                        new string[] {  }) },
                    { "surprised_happy", new EmotionVariant(
                        new string[] { "eyeOpen", "browOuterUp", "mouthOpen", "mouthSmile", "headX" },
                        new Dictionary<string, NumberRange> { { "eyeOpen", new NumberRange(1.05, 1.2) }, { "browOuterUp", new NumberRange(0.2, 0.45) }, { "mouthOpen", new NumberRange(0.12, 0.35) }, { "mouthSmile", new NumberRange(0.55, 0.85) }, { "headX", new NumberRange(-0.04, 0.04) } },
                        new string[] {  }) },
                    { "shy_happy", new EmotionVariant(
                        new string[] { "mouthSmile", "eyeSmile", "gazeX", "gazeY", "blush", "headZ" },
                        new Dictionary<string, NumberRange> { { "mouthSmile", new NumberRange(0.35, 0.65) }, { "eyeSmile", new NumberRange(0.2, 0.45) }, { "gazeX", new NumberRange(-0.35, -0.12) }, { "gazeY", new NumberRange(-0.18, 0.02) }, { "blush", new NumberRange(0.35, 0.85) }, { "headZ", new NumberRange(-0.12, -0.03) } },
                        new string[] { "shy" }) },
                })),
            new KeyValuePair<string, EmotionArchetype>("calm", new EmotionArchetype(
                "calm",
                new string[] { "mouthSmile", "eyeOpen", "eyeSmile", "browInnerUp", "gazeX", "gazeY", "headZ", "bodyY" },
                new Dictionary<string, NumberRange> { { "mouthSmile", new NumberRange(0.08, 0.24) }, { "eyeOpen", new NumberRange(0.82, 0.98) }, { "eyeSmile", new NumberRange(0.08, 0.28) }, { "browInnerUp", new NumberRange(0.0, 0.08) }, { "gazeX", new NumberRange(-0.04, 0.04) }, { "gazeY", new NumberRange(-0.04, 0.02) }, { "headZ", new NumberRange(-0.04, 0.04) }, { "bodyY", new NumberRange(-0.03, 0.03) } },
                new string[] { "soft_calm", "quiet_listen" },
                new Dictionary<string, EmotionVariant>
                {
                    { "soft_calm", new EmotionVariant(
                        new string[] { "mouthSmile", "eyeSmile", "eyeOpen" },
                        new Dictionary<string, NumberRange> { { "mouthSmile", new NumberRange(0.1, 0.22) }, { "eyeSmile", new NumberRange(0.08, 0.22) }, { "eyeOpen", new NumberRange(0.82, 0.94) } },
                        new string[] {  }) },
                    { "quiet_listen", new EmotionVariant(
                        new string[] { "eyeOpen", "browInnerUp", "headY" },
                        new Dictionary<string, NumberRange> { { "eyeOpen", new NumberRange(0.86, 1.0) }, { "browInnerUp", new NumberRange(0.03, 0.1) }, { "headY", new NumberRange(-0.02, 0.05) } },
                        new string[] {  }) },
                })),
            new KeyValuePair<string, EmotionArchetype>("excited", new EmotionArchetype(
                "excited",
                new string[] { "mouthSmile", "mouthOpen", "eyeOpen", "eyeSmile", "browOuterUp", "headX", "headY", "bodyY" },
                new Dictionary<string, NumberRange> { { "mouthSmile", new NumberRange(0.58, 0.95) }, { "mouthOpen", new NumberRange(0.14, 0.42) }, { "eyeOpen", new NumberRange(1.04, 1.24) }, { "eyeSmile", new NumberRange(0.22, 0.56) }, { "browOuterUp", new NumberRange(0.18, 0.48) }, { "headX", new NumberRange(-0.08, 0.08) }, { "headY", new NumberRange(-0.05, 0.08) }, { "bodyY", new NumberRange(-0.06, 0.08) } },
                new string[] { "sparkle", "bounce" },
                new Dictionary<string, EmotionVariant>
                {
                    { "sparkle", new EmotionVariant(
                        new string[] { "mouthSmile", "eyeOpen", "browOuterUp" },
                        new Dictionary<string, NumberRange> { { "mouthSmile", new NumberRange(0.68, 0.95) }, { "eyeOpen", new NumberRange(1.08, 1.24) }, { "browOuterUp", new NumberRange(0.24, 0.56) } },
                        new string[] {  }) },
                    { "bounce", new EmotionVariant(
                        new string[] { "mouthOpen", "headY", "bodyY" },
                        new Dictionary<string, NumberRange> { { "mouthOpen", new NumberRange(0.22, 0.48) }, { "headY", new NumberRange(-0.08, 0.1) }, { "bodyY", new NumberRange(-0.08, 0.1) } },
                        new string[] {  }) },
                })),
            new KeyValuePair<string, EmotionArchetype>("shy", new EmotionArchetype(
                "shy",
                new string[] { "mouthSmile", "eyeOpen", "eyeSmile", "browInnerUp", "gazeX", "gazeY", "headZ", "blush" },
                new Dictionary<string, NumberRange> { { "mouthSmile", new NumberRange(0.2, 0.55) }, { "eyeOpen", new NumberRange(0.82, 1.0) }, { "eyeSmile", new NumberRange(0.16, 0.42) }, { "browInnerUp", new NumberRange(0.04, 0.18) }, { "gazeX", new NumberRange(-0.38, -0.1) }, { "gazeY", new NumberRange(-0.2, 0.02) }, { "headZ", new NumberRange(-0.14, -0.03) }, { "blush", new NumberRange(0.35, 0.9) } },
                new string[] { "bashful", "embarrassed" },
                new Dictionary<string, EmotionVariant>
                {
                    { "bashful", new EmotionVariant(
                        new string[] { "mouthSmile", "eyeSmile", "blush" },
                        new Dictionary<string, NumberRange> { { "mouthSmile", new NumberRange(0.28, 0.58) }, { "eyeSmile", new NumberRange(0.22, 0.46) }, { "blush", new NumberRange(0.52, 0.95) } },
                        new string[] {  }) },
                    { "embarrassed", new EmotionVariant(
                        new string[] { "mouthSmile", "mouthOpen", "browInnerUp", "sweat" },
                        new Dictionary<string, NumberRange> { { "mouthSmile", new NumberRange(0.08, 0.32) }, { "mouthOpen", new NumberRange(0.02, 0.16) }, { "browInnerUp", new NumberRange(0.12, 0.32) }, { "sweat", new NumberRange(0.04, 0.22) } },
                        new string[] {  }) },
                })),
            new KeyValuePair<string, EmotionArchetype>("affectionate", new EmotionArchetype(
                "affectionate",
                new string[] { "mouthSmile", "eyeSmile", "eyeOpen", "browInnerUp", "gazeX", "gazeY", "headZ", "blush" },
                new Dictionary<string, NumberRange> { { "mouthSmile", new NumberRange(0.22, 0.62) }, { "eyeSmile", new NumberRange(0.18, 0.5) }, { "eyeOpen", new NumberRange(0.82, 1.02) }, { "browInnerUp", new NumberRange(0.06, 0.22) }, { "gazeX", new NumberRange(-0.05, 0.05) }, { "gazeY", new NumberRange(-0.02, 0.06) }, { "headZ", new NumberRange(-0.08, 0.08) }, { "blush", new NumberRange(0.08, 0.42) } },
                new string[] { "warm", "close" },
                new Dictionary<string, EmotionVariant>
                {
                    { "warm", new EmotionVariant(
                        new string[] { "mouthSmile", "eyeSmile", "browInnerUp" },
                        new Dictionary<string, NumberRange> { { "mouthSmile", new NumberRange(0.28, 0.6) }, { "eyeSmile", new NumberRange(0.22, 0.48) }, { "browInnerUp", new NumberRange(0.08, 0.24) } },
                        new string[] {  }) },
                    { "close", new EmotionVariant(
                        new string[] { "mouthSmile", "gazeY", "blush" },
                        new Dictionary<string, NumberRange> { { "mouthSmile", new NumberRange(0.18, 0.46) }, { "gazeY", new NumberRange(0.02, 0.08) }, { "blush", new NumberRange(0.2, 0.5) } },
                        new string[] {  }) },
                })),
            new KeyValuePair<string, EmotionArchetype>("curious", new EmotionArchetype(
                "curious",
                new string[] { "browOuterUp", "browInnerUp", "eyeOpen", "mouthOpen", "mouthSmile", "gazeX", "headZ", "headY" },
                new Dictionary<string, NumberRange> { { "browOuterUp", new NumberRange(0.1, 0.34) }, { "browInnerUp", new NumberRange(0.02, 0.18) }, { "eyeOpen", new NumberRange(0.98, 1.16) }, { "mouthOpen", new NumberRange(0.02, 0.18) }, { "mouthSmile", new NumberRange(0.08, 0.28) }, { "gazeX", new NumberRange(-0.16, 0.16) }, { "headZ", new NumberRange(-0.16, 0.16) }, { "headY", new NumberRange(-0.04, 0.06) } },
                new string[] { "tilt", "attentive_question" },
                new Dictionary<string, EmotionVariant>
                {
                    { "tilt", new EmotionVariant(
                        new string[] { "browOuterUp", "headZ", "mouthOpen" },
                        new Dictionary<string, NumberRange> { { "browOuterUp", new NumberRange(0.16, 0.38) }, { "headZ", new NumberRange(-0.18, 0.18) }, { "mouthOpen", new NumberRange(0.04, 0.18) } },
                        new string[] {  }) },
                    { "attentive_question", new EmotionVariant(
                        new string[] { "eyeOpen", "browInnerUp", "gazeX" },
                        new Dictionary<string, NumberRange> { { "eyeOpen", new NumberRange(1.0, 1.18) }, { "browInnerUp", new NumberRange(0.08, 0.22) }, { "gazeX", new NumberRange(-0.04, 0.04) } },
                        new string[] {  }) },
                })),
            new KeyValuePair<string, EmotionArchetype>("concerned", new EmotionArchetype(
                "concerned",
                new string[] { "browInnerUp", "eyeOpen", "mouthSmile", "mouthFrown", "headZ", "gazeX", "gazeY" },
                new Dictionary<string, NumberRange> { { "browInnerUp", new NumberRange(0.22, 0.55) }, { "eyeOpen", new NumberRange(0.78, 0.98) }, { "mouthSmile", new NumberRange(0.04, 0.22) }, { "mouthFrown", new NumberRange(0.05, 0.25) }, { "headZ", new NumberRange(-0.08, 0.08) }, { "gazeX", new NumberRange(-0.05, 0.05) }, { "gazeY", new NumberRange(-0.05, 0.05) } },
                new string[] { "soft_concern", "worried", "comfort" },
                new Dictionary<string, EmotionVariant>
                {
                    { "soft_concern", new EmotionVariant(
                        new string[] { "browInnerUp", "mouthSmile", "eyeOpen" },
                        new Dictionary<string, NumberRange> { { "browInnerUp", new NumberRange(0.2, 0.4) }, { "mouthSmile", new NumberRange(0.08, 0.22) }, { "eyeOpen", new NumberRange(0.82, 0.95) } },
                        new string[] {  }) },
                    { "worried", new EmotionVariant(
                        new string[] { "browInnerUp", "mouthFrown", "eyeOpen", "sweat" },
                        new Dictionary<string, NumberRange> { { "browInnerUp", new NumberRange(0.4, 0.65) }, { "mouthFrown", new NumberRange(0.18, 0.35) }, { "eyeOpen", new NumberRange(0.88, 1.05) }, { "sweat", new NumberRange(0.05, 0.25) } },
                        new string[] {  }) },
                    { "comfort", new EmotionVariant(
                        new string[] { "browInnerUp", "mouthSmile", "eyeSmile", "headZ" },
                        new Dictionary<string, NumberRange> { { "browInnerUp", new NumberRange(0.25, 0.45) }, { "mouthSmile", new NumberRange(0.12, 0.32) }, { "eyeSmile", new NumberRange(0.05, 0.2) }, { "headZ", new NumberRange(-0.06, 0.06) } },
                        new string[] { "warm" }) },
                })),
            new KeyValuePair<string, EmotionArchetype>("tired", new EmotionArchetype(
                "tired",
                new string[] { "eyeOpen", "eyeSquint", "browInnerUp", "mouthFrown", "mouthSmile", "gazeY", "headY", "bodyY" },
                new Dictionary<string, NumberRange> { { "eyeOpen", new NumberRange(0.58, 0.84) }, { "eyeSquint", new NumberRange(0.08, 0.28) }, { "browInnerUp", new NumberRange(0.06, 0.22) }, { "mouthFrown", new NumberRange(0.04, 0.2) }, { "mouthSmile", new NumberRange(0.0, 0.1) }, { "gazeY", new NumberRange(-0.2, -0.04) }, { "headY", new NumberRange(-0.1, -0.02) }, { "bodyY", new NumberRange(-0.08, -0.02) } },
                new string[] { "sleepy", "drained" },
                new Dictionary<string, EmotionVariant>
                {
                    { "sleepy", new EmotionVariant(
                        new string[] { "eyeOpen", "mouthOpen", "headY" },
                        new Dictionary<string, NumberRange> { { "eyeOpen", new NumberRange(0.52, 0.76) }, { "mouthOpen", new NumberRange(0.02, 0.14) }, { "headY", new NumberRange(-0.12, -0.04) } },
                        new string[] {  }) },
                    { "drained", new EmotionVariant(
                        new string[] { "eyeOpen", "browInnerUp", "mouthFrown" },
                        new Dictionary<string, NumberRange> { { "eyeOpen", new NumberRange(0.62, 0.84) }, { "browInnerUp", new NumberRange(0.12, 0.3) }, { "mouthFrown", new NumberRange(0.1, 0.24) } },
                        new string[] {  }) },
                })),
            new KeyValuePair<string, EmotionArchetype>("sad", new EmotionArchetype(
                "sad",
                new string[] { "browInnerUp", "eyeOpen", "eyeSquint", "mouthFrown", "mouthSmile", "gazeY", "headY", "tear" },
                new Dictionary<string, NumberRange> { { "browInnerUp", new NumberRange(0.28, 0.6) }, { "eyeOpen", new NumberRange(0.64, 0.92) }, { "eyeSquint", new NumberRange(0.04, 0.2) }, { "mouthFrown", new NumberRange(0.18, 0.5) }, { "mouthSmile", new NumberRange(0.0, 0.08) }, { "gazeY", new NumberRange(-0.24, -0.06) }, { "headY", new NumberRange(-0.12, -0.02) }, { "tear", new NumberRange(0.0, 0.32) } },
                new string[] { "downcast", "teary" },
                new Dictionary<string, EmotionVariant>
                {
                    { "downcast", new EmotionVariant(
                        new string[] { "browInnerUp", "mouthFrown", "gazeY" },
                        new Dictionary<string, NumberRange> { { "browInnerUp", new NumberRange(0.3, 0.56) }, { "mouthFrown", new NumberRange(0.22, 0.48) }, { "gazeY", new NumberRange(-0.26, -0.08) } },
                        new string[] {  }) },
                    { "teary", new EmotionVariant(
                        new string[] { "browInnerUp", "eyeOpen", "tear" },
                        new Dictionary<string, NumberRange> { { "browInnerUp", new NumberRange(0.38, 0.68) }, { "eyeOpen", new NumberRange(0.68, 0.95) }, { "tear", new NumberRange(0.22, 0.58) } },
                        new string[] {  }) },
                })),
            new KeyValuePair<string, EmotionArchetype>("anxiety", new EmotionArchetype(
                "anxiety",
                new string[] { "browInnerUp", "browOuterUp", "eyeOpen", "mouthFrown", "mouthOpen", "gazeX", "headZ", "sweat" },
                new Dictionary<string, NumberRange> { { "browInnerUp", new NumberRange(0.24, 0.58) }, { "browOuterUp", new NumberRange(0.08, 0.32) }, { "eyeOpen", new NumberRange(1.02, 1.22) }, { "mouthFrown", new NumberRange(0.12, 0.36) }, { "mouthOpen", new NumberRange(0.02, 0.18) }, { "gazeX", new NumberRange(-0.22, 0.22) }, { "headZ", new NumberRange(-0.12, 0.12) }, { "sweat", new NumberRange(0.12, 0.46) } },
                new string[] { "nervous", "uneasy" },
                new Dictionary<string, EmotionVariant>
                {
                    { "nervous", new EmotionVariant(
                        new string[] { "eyeOpen", "browInnerUp", "sweat" },
                        new Dictionary<string, NumberRange> { { "eyeOpen", new NumberRange(1.04, 1.22) }, { "browInnerUp", new NumberRange(0.32, 0.62) }, { "sweat", new NumberRange(0.18, 0.52) } },
                        new string[] {  }) },
                    { "uneasy", new EmotionVariant(
                        new string[] { "gazeX", "mouthFrown", "headZ" },
                        new Dictionary<string, NumberRange> { { "gazeX", new NumberRange(-0.28, 0.28) }, { "mouthFrown", new NumberRange(0.16, 0.38) }, { "headZ", new NumberRange(-0.16, 0.16) } },
                        new string[] {  }) },
                })),
            new KeyValuePair<string, EmotionArchetype>("confused", new EmotionArchetype(
                "confused",
                new string[] { "browInnerUp", "browDown", "eyeOpen", "mouthOpen", "mouthFrown", "gazeX", "headZ" },
                new Dictionary<string, NumberRange> { { "browInnerUp", new NumberRange(0.08, 0.28) }, { "browDown", new NumberRange(0.08, 0.25) }, { "eyeOpen", new NumberRange(0.92, 1.1) }, { "mouthOpen", new NumberRange(0.02, 0.16) }, { "mouthFrown", new NumberRange(0.04, 0.18) }, { "gazeX", new NumberRange(-0.18, 0.18) }, { "headZ", new NumberRange(-0.14, 0.14) } },
                new string[] { "confused" },
                new Dictionary<string, EmotionVariant>
                {
                    { "confused", new EmotionVariant(
                        new string[] { "browInnerUp", "browDown", "mouthOpen", "headZ" },
                        new Dictionary<string, NumberRange> { { "browInnerUp", new NumberRange(0.12, 0.3) }, { "browDown", new NumberRange(0.08, 0.24) }, { "mouthOpen", new NumberRange(0.04, 0.18) }, { "headZ", new NumberRange(-0.16, 0.16) } },
                        new string[] {  }) },
                })),
            new KeyValuePair<string, EmotionArchetype>("surprised", new EmotionArchetype(
                "surprised",
                new string[] { "eyeOpen", "browOuterUp", "mouthOpen", "mouthSmile", "gazeX", "gazeY", "headX" },
                new Dictionary<string, NumberRange> { { "eyeOpen", new NumberRange(1.08, 1.22) }, { "browOuterUp", new NumberRange(0.28, 0.55) }, { "mouthOpen", new NumberRange(0.18, 0.42) }, { "mouthSmile", new NumberRange(0.0, 0.18) }, { "gazeX", new NumberRange(-0.03, 0.03) }, { "gazeY", new NumberRange(-0.02, 0.04) }, { "headX", new NumberRange(-0.05, 0.05) } },
                new string[] { "startled" },
                new Dictionary<string, EmotionVariant>
                {
                    { "startled", new EmotionVariant(
                        new string[] { "eyeOpen", "browOuterUp", "mouthOpen", "headY" },
                        new Dictionary<string, NumberRange> { { "eyeOpen", new NumberRange(1.12, 1.24) }, { "browOuterUp", new NumberRange(0.34, 0.6) }, { "mouthOpen", new NumberRange(0.2, 0.45) }, { "headY", new NumberRange(-0.05, 0.02) } },
                        new string[] {  }) },
                })),
            new KeyValuePair<string, EmotionArchetype>("anger", new EmotionArchetype(
                "anger",
                new string[] { "browDown", "eyeOpen", "eyeSquint", "mouthFrown", "mouthOpen", "gazeX", "headZ", "sweat" },
                new Dictionary<string, NumberRange> { { "browDown", new NumberRange(0.28, 0.6) }, { "eyeOpen", new NumberRange(0.76, 0.98) }, { "eyeSquint", new NumberRange(0.12, 0.38) }, { "mouthFrown", new NumberRange(0.18, 0.46) }, { "mouthOpen", new NumberRange(0.0, 0.16) }, { "gazeX", new NumberRange(-0.04, 0.04) }, { "headZ", new NumberRange(-0.08, 0.08) }, { "sweat", new NumberRange(0.0, 0.18) } },
                new string[] { "annoyed", "firm" },
                new Dictionary<string, EmotionVariant>
                {
                    { "annoyed", new EmotionVariant(
                        new string[] { "browDown", "eyeSquint", "mouthFrown" },
                        new Dictionary<string, NumberRange> { { "browDown", new NumberRange(0.22, 0.44) }, { "eyeSquint", new NumberRange(0.08, 0.26) }, { "mouthFrown", new NumberRange(0.12, 0.34) } },
                        new string[] {  }) },
                    { "firm", new EmotionVariant(
                        new string[] { "browDown", "eyeSquint", "mouthFrown", "headY" },
                        new Dictionary<string, NumberRange> { { "browDown", new NumberRange(0.36, 0.62) }, { "eyeSquint", new NumberRange(0.18, 0.42) }, { "mouthFrown", new NumberRange(0.24, 0.48) }, { "headY", new NumberRange(0.02, 0.08) } },
                        new string[] {  }) },
                })),
            new KeyValuePair<string, EmotionArchetype>("angry", new EmotionArchetype(
                "angry",
                new string[] { "browDown", "eyeOpen", "eyeSquint", "mouthFrown", "mouthOpen", "gazeX", "headZ", "sweat" },
                new Dictionary<string, NumberRange> { { "browDown", new NumberRange(0.28, 0.58) }, { "eyeOpen", new NumberRange(0.78, 0.96) }, { "eyeSquint", new NumberRange(0.1, 0.35) }, { "mouthFrown", new NumberRange(0.18, 0.42) }, { "mouthOpen", new NumberRange(0.0, 0.12) }, { "gazeX", new NumberRange(-0.04, 0.04) }, { "headZ", new NumberRange(-0.08, 0.08) }, { "sweat", new NumberRange(0.0, 0.16) } },
                new string[] { "annoyed" },
                new Dictionary<string, EmotionVariant>
                {
                    { "annoyed", new EmotionVariant(
                        new string[] { "browDown", "eyeSquint", "mouthFrown" },
                        new Dictionary<string, NumberRange> { { "browDown", new NumberRange(0.22, 0.42) }, { "eyeSquint", new NumberRange(0.08, 0.24) }, { "mouthFrown", new NumberRange(0.12, 0.32) } },
                        new string[] {  }) },
                })),
        };

        private static readonly Dictionary<string, EmotionArchetype> Map = BuildMap();

        private static Dictionary<string, EmotionArchetype> BuildMap()
        {
            var outp = new Dictionary<string, EmotionArchetype>(List.Count);
            foreach (var kv in List) outp[kv.Key] = kv.Value;
            return outp;
        }

        /// <summary>`getEmotionArchetype`：不認得的情緒名回 neutral（照上游行為）。</summary>
        public static EmotionArchetype Get(string emotion) =>
            Map.TryGetValue(emotion ?? "", out var a) ? a : Map["neutral"];
    }
}
