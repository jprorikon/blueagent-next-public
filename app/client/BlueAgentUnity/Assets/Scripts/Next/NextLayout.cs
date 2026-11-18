using BlueAgent.Screens;
using UnityEngine;

namespace BlueAgent.Next
{
    /// <summary>
    /// 大螢幕適配（owner 2026-10-30「iPhone Duo 的展開屏幕適配」）。
    ///
    /// iPhone Duo 有兩塊螢幕：摺起來的外屏（約 466×678 pt，直）與展開的內屏（約 951×669 pt，**天生是橫的**）。
    /// 以前整個 app 鎖直向、畫布永遠是 460 設計單位寬 ⇒ 展開之後被系統縮成中間一條、兩邊黑邊。
    ///
    /// 規則：
    /// <list type="bullet">
    /// <item>**大螢幕**＝整塊螢幕短邊 ≥ <see cref="LargeShortSide"/> pt（Duo 內屏 669；外屏 466、一般手機 ≤ 440 都不算）；</item>
    /// <item>只有大螢幕才放開橫向（手機橫過來聊天很擠，維持直向）；</item>
    /// <item>畫布：手機照舊 460 寬（貼合螢幕寬）；大螢幕 1 設計單位≈1 pt（不再把 460 放大成一條）；</item>
    /// <item>聊天頁夠寬（≥ <see cref="TwoPaneMinWidth"/>）就左右兩欄：左邊立繪＋角色、右邊對話；</item>
    /// <item>其他頁（設定、相冊、通話…）置中、最寬 <see cref="PageMaxWidth"/>，不拉成一整排。</item>
    /// </list>
    /// </summary>
    public static class NextLayout
    {
        /// <summary>整塊螢幕短邊超過這個（pt）才算大螢幕。</summary>
        public const float LargeShortSide = 600f;

        /// <summary>畫布寬到這個（設計單位）聊天頁就分兩欄。</summary>
        public const float TwoPaneMinWidth = 760f;

        /// <summary>聊天以外的頁面最寬多少（設計單位）。</summary>
        public const float PageMaxWidth = 600f;

#if UNITY_IOS && !UNITY_EDITOR
        [System.Runtime.InteropServices.DllImport("__Internal")] private static extern float ba_screen_scale();
        [System.Runtime.InteropServices.DllImport("__Internal")] private static extern float ba_screen_short_side();
#endif

        /// <summary>每 pt（Android 是 dp）幾個像素。</summary>
        public static float PixelsPerPoint()
        {
#if UNITY_IOS && !UNITY_EDITOR
            var scale = ba_screen_scale();
            return scale > 0f ? scale : 3f;
#elif UNITY_ANDROID && !UNITY_EDITOR
            return Screen.dpi > 0f ? Screen.dpi / 160f : 2.75f;
#else
            return Screen.dpi > 150f ? 2f : 1f;
#endif
        }

        /// <summary>整塊螢幕的短邊（pt）：iOS 問原生（見 `BlueAgentScreen.mm` 為什麼不能用 `Screen`）。</summary>
        public static float ShortSidePoints()
        {
#if UNITY_IOS && !UNITY_EDITOR
            var side = ba_screen_short_side();
            if (side > 0f) return side;
#endif
            return Mathf.Min(Screen.width, Screen.height) / PixelsPerPoint();
        }

        public static bool IsLargeScreen()
        {
            return ShortSidePoints() >= LargeShortSide;
        }

        /// <summary>
        /// 畫布要幾個設計單位寬：手機回 0（＝照舊用 460）；大螢幕回 app 這一塊的寬（pt），
        /// 也就是 1 設計單位≈1 pt（手機上 1 單位≈0.85 pt，大螢幕的字因此略大一點，剛好）。
        /// </summary>
        public static float DesignWidth()
        {
            if (!IsLargeScreen()) return 0f;
            return Mathf.Max(UiBuilder.Reference.x, Screen.width / PixelsPerPoint());
        }

        public static bool TwoPane(float canvasWidth)
        {
            return canvasWidth >= TwoPaneMinWidth;
        }

        /// <summary>大螢幕才放開橫向；手機（含 Duo 摺起來的外屏）維持直向。</summary>
        public static void ApplyOrientation()
        {
            AllowLandscape(IsLargeScreen());
        }

        /// <summary>
        /// 直接開／關橫向。螢幕一變就先**開**（試探），等一下再用 <see cref="ApplyOrientation"/> 決定要不要收：
        /// 摺著開 app ⇒ 只准直向 ⇒ 展開後系統把 app 縮成中間一條，**縮著的時候 app 量不到真正的螢幕**
        /// （`Screen`、`UIScreen.bounds`、連 `nativeBounds` 都回縮過的 469 pt，模擬器實測）。
        /// 先放開橫向，系統才會讓 app 鋪滿內屏、量到 669 pt。
        /// </summary>
        public static void AllowLandscape(bool allow)
        {
            Screen.autorotateToPortrait = true;
            Screen.autorotateToPortraitUpsideDown = false;
            Screen.autorotateToLandscapeLeft = allow;
            Screen.autorotateToLandscapeRight = allow;
            if (Screen.orientation != ScreenOrientation.AutoRotation) Screen.orientation = ScreenOrientation.AutoRotation;
        }
    }
}
