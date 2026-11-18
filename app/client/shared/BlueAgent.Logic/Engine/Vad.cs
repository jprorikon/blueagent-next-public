using System;
using System.Collections.Generic;

namespace BlueAgent.Logic.Engine
{
    /// <summary>
    /// VAD 三軸向量 —— C# 版，對照 Kotlin `emotion/VadState.kt`（`emotion/VADState.ts`）。
    ///
    /// VAD ＝ Valence（愉悅度）／Arousal（喚起度）／Dominance（支配感），三軸各 −1..1。
    /// 這是整個情緒系統的**狀態載體**：情緒事件只改 VAD，表現層再從 VAD 反推表情、微動、手勢、
    /// 私有參數 —— 所以 VAD 一旦漂了，後面每一層都會跟著漂。
    /// </summary>
    public readonly struct VadVector
    {
        public readonly double Valence;
        public readonly double Arousal;
        public readonly double Dominance;

        public VadVector(double valence, double arousal, double dominance)
        {
            Valence = valence;
            Arousal = arousal;
            Dominance = dominance;
        }
    }

    /// <summary>TS 的 `Partial&lt;VADVector&gt;`：只指定部分軸，其餘沿用既有值（null ＝ 沿用）。</summary>
    public sealed class VadPartial
    {
        public readonly double? Valence;
        public readonly double? Arousal;
        public readonly double? Dominance;

        public VadPartial(double? valence = null, double? arousal = null, double? dominance = null)
        {
            Valence = valence;
            Arousal = arousal;
            Dominance = dominance;
        }
    }

    /// <summary>
    /// `VADRuntimeState` —— `EmotionStateController.Update()` 的回傳。
    /// ⚠️ 這個型別**不是**向量：要取的是 <see cref="Current"/>。
    /// </summary>
    public sealed class VadRuntimeState
    {
        public readonly VadVector Current;
        public readonly VadVector Target;
        public readonly string DominantEmotion;
        public readonly double Intensity;
        public readonly VadVector Ambient;
        public readonly double HoldSeconds;
        public readonly double DecayRate;

        public VadRuntimeState(
            VadVector current,
            VadVector target,
            string dominantEmotion,
            double intensity,
            // 與 Kotlin 的預設值一致（`ambient`／`holdSeconds`／`decayRate` 都可以省）
            VadVector ambient = default,
            double holdSeconds = 0.0,
            double decayRate = 0.0)
        {
            Current = current;
            Target = target;
            DominantEmotion = dominantEmotion;
            Intensity = intensity;
            Ambient = ambient;
            HoldSeconds = holdSeconds;
            DecayRate = decayRate;
        }
    }

    /// <summary>
    /// `reaction/EmotionIntent`：一次「情緒事件」。
    ///
    /// 這是外部（橋側／分類器）唯一需要交給表現引擎的東西 —— 情緒名 ＋ 強度，
    /// 其餘欄位是選配的覆寫。VAD 狀態機只吃 emotion／variant／intensity／natural*。
    /// </summary>
    public sealed class EmotionIntent
    {
        public readonly string Emotion;
        public readonly string Variant;
        /// <summary>「自然情緒」：與演出用的 emotion 分開，用在 VAD 與殘響上。</summary>
        public readonly string NaturalEmotion;
        public readonly string NaturalVariant;
        public readonly VadPartial NaturalVad;
        public readonly double Intensity;
        public readonly IReadOnlyList<string> ContextTags;
        /// <summary>觸發這句話的原文（用來估說話時長；缺席時上游拿情緒名當替代）。</summary>
        public readonly string SourceMessage;

        public EmotionIntent(
            string emotion,
            string variant,
            string naturalEmotion,
            string naturalVariant,
            VadPartial naturalVad,
            double intensity,
            IReadOnlyList<string> contextTags = null,
            string sourceMessage = null)
        {
            Emotion = emotion;
            Variant = variant;
            NaturalEmotion = naturalEmotion;
            NaturalVariant = naturalVariant;
            NaturalVad = naturalVad;
            Intensity = intensity;
            ContextTags = contextTags ?? new List<string>();
            SourceMessage = sourceMessage;
        }
    }

    /// <summary>VAD 的共用小工具（Kotlin 是同檔的 private 頂層函式，C# 收成靜態類）。</summary>
    public static class Vad
    {
        public static readonly VadVector Neutral = new VadVector(0.0, 0.0, 0.0);

        public static VadVector Clamp(VadVector v) => new VadVector(
            EngineMath.Clamp(v.Valence, -1.0, 1.0),
            EngineMath.Clamp(v.Arousal, -1.0, 1.0),
            EngineMath.Clamp(v.Dominance, -1.0, 1.0));

        public static VadVector Lerp(VadVector from, VadVector to, double amount) => Clamp(new VadVector(
            EngineMath.Lerp(from.Valence, to.Valence, amount),
            EngineMath.Lerp(from.Arousal, to.Arousal, amount),
            EngineMath.Lerp(from.Dominance, to.Dominance, amount)));

        /// <summary>
        /// 這個「強度」的權重是刻意的：0.82／0.64／2.46 三個常數都不要動。
        /// ⚠️ `emotion/VadMicroMotionController` 用的是 **0.62**（不是 0.64）——兩支長得像，別抄錯。
        /// </summary>
        public static double Magnitude(VadVector v) => EngineMath.Clamp(
            (Math.Abs(v.Valence) + Math.Abs(v.Arousal) * 0.82 + Math.Abs(v.Dominance) * 0.64) / 2.46, 0.0, 1.0);

        /// <summary>`completeVad(value, fallback)`：只補「沒給的軸」。</summary>
        public static VadVector Complete(VadPartial value, VadVector fallback) => Clamp(new VadVector(
            value?.Valence ?? fallback.Valence,
            value?.Arousal ?? fallback.Arousal,
            value?.Dominance ?? fallback.Dominance));
    }
}
