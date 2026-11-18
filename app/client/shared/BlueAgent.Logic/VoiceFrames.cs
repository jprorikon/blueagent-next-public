using System;

namespace BlueAgent.Logic
{
    /// <summary>
    /// 「按住說話」實際錄到幾個樣本（2026-10-29）：Unity `Microphone.GetPosition` 的讀數不可全信，
    /// 這裡把它卡在合理範圍內。畫面上的秒數與送出去的音檔都用這一個數，兩邊不會再脫節
    /// （以前一邊用牆鐘、一邊用 GetPosition ⇒「按住 3 秒卻顯示 60.0s」）。
    /// 放在 Logic：純計算，`dotnet test` 釘得住；`NextVoice` 只負責去問麥克風。
    /// </summary>
    public static class VoiceFrames
    {
        /// <summary>牆鐘之外多給的餘量（秒）：緩衝延遲，讀數略超前也算合理。</summary>
        public const float Slack = 0.5f;

        /// <param name="position">`Microphone.GetPosition` 的讀數（拿不到就傳 0）。</param>
        /// <param name="clipSamples">緩衝全長（＝最長秒數 × 取樣率）。</param>
        /// <param name="elapsedSeconds">從開錄到現在的牆鐘秒數。</param>
        /// <param name="maxSeconds">緩衝能裝的最長秒數。</param>
        /// <param name="frequency">緩衝的取樣率。</param>
        /// <returns>
        /// 0..<paramref name="clipSamples"/>：
        /// 讀數 ≤ 0 ⇒ 還沒錄滿就當沒錄到（剛按下／裝置異常），錄滿了（非循環緩衝停住、部分平台歸零）才算整顆；
        /// 讀數 &gt; 0 ⇒ 不超過緩衝全長，也不超過「經過的時間 ＋ 餘量」（有的裝置會回整個緩衝長度）。
        /// </returns>
        public static int Recorded(int position, int clipSamples, float elapsedSeconds, float maxSeconds, int frequency)
        {
            if (clipSamples <= 0 || frequency <= 0) return 0;
            if (position <= 0) return elapsedSeconds >= maxSeconds ? clipSamples : 0;
            var ceiling = (int)((Math.Max(0f, elapsedSeconds) + Slack) * frequency);
            return Math.Min(position, Math.Min(clipSamples, ceiling));
        }
    }
}
