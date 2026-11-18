using System;
using System.Collections.Generic;

namespace BlueAgent.Logic.Engine
{
    /// <summary>
    /// **量測音量**的來源介面 —— C# 版，對照 Kotlin `speech/AudioLevelAnalyzer.kt`。
    ///
    /// 為什麼要有這個介面：口型層有兩條路 ——
    ///  * 拿不到量測值（沒開語音／來源暫時斷線）⇒ 走**程序化**那條（純函式，照時間算音節）；
    ///  * 拿得到 ⇒ 走**量測**那條（RMS 過 attack／release 平滑 ＋ 峰值算重音）。
    ///
    /// Kotlin 的介面帶預設實作（`getPeak` 回 null、`isAvailable` 回 true、`reset` 空）；
    /// C# 的介面要 C# 8 預設方法，這個專案統一用**抽象類別 ＋ virtual**，語意相同。
    /// </summary>
    public abstract class AudioLevelAnalyzer
    {
        /// <summary>正規化 RMS／音量（0..1）。</summary>
        public abstract double GetLevel();

        /// <summary>選配：正規化瞬時峰值（0..1）。沒有就回 null。</summary>
        public virtual double? GetPeak() => null;

        /// <summary>來源現在有沒有訊號；暫時斷線時回 false。</summary>
        public virtual bool IsAvailable() => true;

        /// <summary>說話結束時清掉來源側的平滑／緩衝。</summary>
        public virtual void Reset() { }
    }

    /// <summary>
    /// `NullAudioLevelAnalyzer`：永遠沒有訊號的來源。
    /// ⚠️ 它跟「沒有設定分析器」在 runtime 裡**等價**（兩個都會讓口型走程序化那條路），
    /// 但語意不同：這個是「有來源、只是沒東西」，null 是「沒有來源」。
    /// </summary>
    public sealed class NullAudioLevelAnalyzer : AudioLevelAnalyzer
    {
        public override double GetLevel() => 0.0;

        public override double? GetPeak() => 0.0;

        public override bool IsAvailable() => false;

        public override void Reset() { }
    }

    /// <summary>
    /// `speech/MockSpeechController.ts`：說話時長 ＝ `min(4.6, 1.2 + 長度*0.08)`，長度下限 6
    /// —— 也就是最短 1.68 秒、最長 4.6 秒。
    /// </summary>
    public static class MockSpeech
    {
        public static double EstimateMockSpeechDuration(string message)
        {
            var text = message ?? "";
            var length = Math.Max(6, KotlinText.Trim(text).Length);
            return Math.Min(4.6, 1.2 + length * 0.08);
        }
    }

    /// <summary>`motion` 的一格：`AtMs` ＝ 相對這一句起點的毫秒數。</summary>
    public sealed class MouthShapeFrame
    {
        public readonly double AtMs;
        public readonly string Shape;

        public MouthShapeFrame(double atMs, string shape)
        {
            AtMs = atMs;
            Shape = shape;
        }
    }

    /// <summary>
    /// **橋側嘴型腳本**（`speech.motion` 的形狀）的取樣器 —— C# 版，對照 Kotlin `speech/MouthScript.kt`。
    ///
    /// 契約：橋在 `speech` 幀裡連音訊一起回 `motion: [[ms, shape], …]`（毫秒**相對這一句音訊的起點**）。
    /// 手機照**播放頭**取用 —— 腳本自己不會跑，時間由播放器給。
    ///
    /// 這一層是引擎口型的**主路**（橋有給就用）；橋沒給／舊版橋 ⇒ 交回引擎自己的量測／程序化口型
    /// （見 <see cref="LipSyncController"/>）—— 也就是「本機回退」。
    /// </summary>
    public static class MouthScript
    {
        /// <summary>認得的形狀（與網頁版一致）。不認得的形狀會當成 `closed`。</summary>
        public static readonly IReadOnlyList<string> MouthShapes = new[] { "closed", "a", "i", "u", "e", "o" };

        /// <summary>
        /// 形狀 → 嘴開度（0..1）。
        /// ⚠️ **占位值**：這是「看起來像在講那個人聲」的粗對應，不是量測值；owner 定版前不要當成定論。
        /// 要調就只動這一張表（其他地方都不寫死數字）。
        /// </summary>
        public static readonly IReadOnlyDictionary<string, float> MouthShapeOpen = new Dictionary<string, float>
        {
            { "closed", 0.00f },
            { "a", 0.85f },
            { "i", 0.35f },
            { "u", 0.30f },
            { "e", 0.55f },
            { "o", 0.70f },
        };

        /// <summary>
        /// 依播放頭取樣（**階梯式**：保持上一個形狀，直到下一個關鍵格）。
        /// * 播放頭在**第一個關鍵格之前** ⇒ `closed`（0）—— 主播還沒開口，不該張嘴；
        /// * 播放頭在**最後一個關鍵格之後** ⇒ 保持最後那個形狀。
        ///
        /// <paramref name="frames"/> 需**依 `AtMs` 升序**（<see cref="MouthScriptTrack.Start"/> 會先排好）。
        /// </summary>
        public static float SampleMouthScript(IReadOnlyList<MouthShapeFrame> frames, double playbackMs)
        {
            var current = "closed";
            foreach (var frame in frames)
            {
                if (frame.AtMs > playbackMs) break;
                current = frame.Shape;
            }
            // ⚠️ **嚴格比對，不做大小寫正規化**（與 Kotlin 參考實作逐字對拍）。
            //    2026-10-12 真機踩到的手寫 fixture 用大寫（`A`／`I`／`U`）⇒ 全落在「認不得 ⇒ closed」
            //    那條安全路上（狀態機照演、聲音照放、只有嘴不動）；修法是**fixture 改用真橋抓的**
            //    （`tools/make_replay_fixture.py`，線上送小寫），不是在這裡放寬比對
            //    ——放寬會把「上游形狀名漂了」這個訊號一起吃掉（`UnknownShapes` 就是設計來看它的）。
            return MouthShapeOpen.TryGetValue(current ?? "", out var open) ? open : 0f;
        }
    }

    /// <summary>
    /// 持有「這一句」的嘴型腳本。宿主在**句子開始**時 <see cref="Start"/>、播放中每幀
    /// <see cref="Sample"/>、句子結束時 <see cref="Clear"/>。
    /// **沒腳本時 <see cref="Sample"/> 回 `null`** ⇒ 呼叫方把 `null` 交給宿主口型介面，
    /// 就自動退回引擎自己的口型層（本機回退）。
    /// </summary>
    public sealed class MouthScriptTrack
    {
        private IReadOnlyList<MouthShapeFrame> _frames = new List<MouthShapeFrame>();

        /// <summary>這一句裡認不得的形狀（去重）。出現這個＝橋側加了新形狀而我們還不認得。</summary>
        public IReadOnlyList<string> UnknownShapes { get; private set; } = new List<string>();

        public bool IsActive => _frames.Count > 0;

        /// <summary>這一句的關鍵格數（給診斷用）。</summary>
        public int FrameCount => _frames.Count;

        public void Start(IReadOnlyList<MouthShapeFrame> raw)
        {
            // 橋側是照時間序回的，但排序成本極低 ⇒ 不吃「上游一定排好」這個假設
            // ⚠️ 要用**穩定**排序（LINQ OrderBy）；List.Sort 不穩定，同 AtMs 的順序會亂掉
            var sorted = new List<MouthShapeFrame>(raw ?? new List<MouthShapeFrame>());
            _frames = new List<MouthShapeFrame>(System.Linq.Enumerable.OrderBy(sorted, f => f.AtMs));

            var unknown = new List<string>();
            foreach (var f in raw ?? new List<MouthShapeFrame>())
            {
                var shape = f.Shape;
                if (Contains(MouthScript.MouthShapes, shape) || Contains(unknown, shape)) continue;
                unknown.Add(shape);
            }
            UnknownShapes = unknown;
        }

        public void Clear()
        {
            _frames = new List<MouthShapeFrame>();
            UnknownShapes = new List<string>();
        }

        /// <summary>播放頭取樣。沒腳本 ⇒ `null`（＝交回引擎口型層）。</summary>
        public float? Sample(double playbackMs) =>
            _frames.Count == 0 ? (float?)null : MouthScript.SampleMouthScript(_frames, playbackMs);

        private static bool Contains(IReadOnlyList<string> list, string value)
        {
            for (var i = 0; i < list.Count; i++)
            {
                if (list[i] == value) return true;
            }
            return false;
        }
    }
}
