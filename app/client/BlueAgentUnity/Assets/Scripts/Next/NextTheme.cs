using UnityEngine;

namespace BlueAgent.Next
{
    /// <summary>
    /// Next 的設計記號（2026-10-29 改版）：參考 an original geometric UI 的**設計語言**——明亮的天藍、深海軍藍字、
    /// 黃色主行動鈕、白面板、斜切的平行四邊形按鈕、光環／三角碎片的裝飾——但所有素材都是自己畫的
    /// （圖示與裝飾見 `tools/bake_next_icons.py`），不用遊戲的圖、字型或音效。
    ///
    /// 規則：畫面上的顏色**只從這裡拿**（以前 `e3eef8` 這種值在各頁重複寫了幾十次）；
    /// 按鈕不自己挑顏色，挑 <see cref="NextUi.Style"/>。
    /// </summary>
    public static class NextTheme
    {
        private static Color Hex(string hex)
        {
            ColorUtility.TryParseHtmlString("#" + hex, out var color);
            return color;
        }

        // ── 底色 ──
        public static readonly Color PageTop = Hex("f3f9fe");
        public static readonly Color PageBottom = Hex("dcedf9");
        public static readonly Color Pattern = new Color(1f, 1f, 1f, 0.28f);
        public static readonly Color Panel = new Color(1f, 1f, 1f, 0.96f);
        public static readonly Color PanelLine = Hex("cfe1ee");
        public static readonly Color Divider = Hex("e4eef6");
        public static readonly Color Shadow = new Color(0.17f, 0.27f, 0.38f, 0.16f);

        // ── 字 ──
        public static readonly Color Ink = Hex("2b4462");
        public static readonly Color InkSoft = Hex("6e89a3");
        public static readonly Color InkFaint = Hex("9fb3c6");
        /// <summary>系統標記（例：通話結束那一行）的字色：比正文淡一級。</summary>
        public static readonly Color MarkerInk = Hex("9fb3c6");
        public static readonly Color OnColor = Color.white;

        // ── 語意色 ──
        public static readonly Color Sky = Hex("3ea7f2");
        public static readonly Color SkyTop = Hex("63c2fa");
        public static readonly Color SkyDeep = Hex("1f7fd0");
        public static readonly Color SkyPale = Hex("e4f3fd");
        public static readonly Color Navy = Hex("2b4462");
        public static readonly Color NavyTop = Hex("3b5a7e");
        public static readonly Color Yellow = Hex("ffd84a");
        public static readonly Color YellowTop = Hex("ffe988");
        public static readonly Color Pink = Hex("f5839b");
        public static readonly Color PinkPale = Hex("fde7ec");
        public static readonly Color Danger = Hex("e0475f");
        public static readonly Color DangerPale = Hex("fdebee");
        public static readonly Color Ok = Hex("2fb383");
        public static readonly Color OkPale = Hex("e2f6ee");
        public static readonly Color Warn = Hex("e59b2b");
        public static readonly Color WarnPale = Hex("fff2dc");
        public static readonly Color Idle = Hex("aebfcf");
        public static readonly Color IdlePale = Hex("edf2f6");

        // ── 聊天（MomoTalk 的排法：她＝深灰藍、我＝天藍，字都是白的）──
        public static readonly Color HerBubble = Hex("4a5a70");
        public static readonly Color MineBubble = Hex("3f98e6");
        public static readonly Color Composer = new Color(1f, 1f, 1f, 0.97f);
        public static readonly Color Field = Hex("f1f7fc");

        // ── 形 ──
        /// <summary>斜切按鈕的剪切量（每 1 單位高往右偏多少）：≈ 11°。</summary>
        public const float Skew = 0.2f;
        public const float Radius = 8f;
        public const float SmallRadius = 5f;

        // ── 字級（設計單位：畫布寬 460）──
        // owner 2026-10-30 放大一級：以前正文 12.5（iPhone 上約 10.7 pt）、小字 9.5（約 8 pt），
        // 低於 iOS 建議的 11 pt 下限，看久了吃力。
        public const float TitleSize = 19f;
        public const float HeadSize = 15f;
        public const float BodySize = 15f;
        public const float LabelSize = 14f;
        public const float NoteSize = 12f;
        public const float TinySize = 11f;
        /// <summary>系統標記（例：通話結束那一行、日期分隔）：比正文再小一號。</summary>
        public const float MarkerSize = 12f;

        // ── 點擊目標（設計單位）──
        /// <summary>圖示鈕預設大小：以前 32～36（約 27～31 pt），低於 iOS 建議的 44 pt，常按不準。</summary>
        public const float TapSize = 42f;

        // ── 動 ──
        public const float EnterSeconds = 0.26f;
        public const float StaggerSeconds = 0.035f;
        public const float PressScale = 0.93f;
    }
}
