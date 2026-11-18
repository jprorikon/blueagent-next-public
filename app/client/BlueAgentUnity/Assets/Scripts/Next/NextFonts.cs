using System.Collections.Generic;
using BlueAgent.Logic;
using BlueAgent.Screens;
using TMPro;
using UnityEngine;

namespace BlueAgent.Next
{
    /// <summary>
    /// 按語言挑字型（2026-10-28）：界面字跟界面語言走 Noto Sans JP／SC／TC，**她說的話與角色名一律日語字型**。
    ///
    /// 為什麼要分：以前中日文全由 `DroidSansFallback`（中文字形）畫 ⇒ 日語漢字也是中文寫法（「直」「骨」「写」），
    /// 就算字全是日語，看起來還是像混著中文。同一個漢字在日／簡／繁的字形不一樣，只能按語言換整支字型。
    ///
    /// 檔案：`Resources/Fonts/NotoSans{JP,SC,TC}-Regular.ttf`（google/fonts 的可變字型切出 wght=400 的靜態實例，
    /// OFL，授權文在同目錄 `NotoSansCJK-OFL.txt`）。粗體由 TMP 合成。
    /// 記憶體：用到哪支才載哪支（界面中文時＝SC＋JP 兩支）；缺字退回 `DroidSansFallback`。
    /// </summary>
    public static class NextFonts
    {
        private static readonly Dictionary<string, TMP_FontAsset> Loaded = new Dictionary<string, TMP_FontAsset>();
        private static TMP_FontAsset _fallback;
        private static bool _fallbackTried;

        private static string FileFor(string locale)
        {
            switch (NextText.Normalize(locale))
            {
                case NextText.ZhHans: return "Fonts/NotoSansSC-Regular";
                case NextText.ZhHant: return "Fonts/NotoSansTC-Regular";
                default: return "Fonts/NotoSansJP-Regular";
            }
        }

        /// <summary>這一種語言的字型；建不出來回 null（呼叫端照舊用原本那支）。</summary>
        public static TMP_FontAsset For(string locale)
        {
            var file = FileFor(locale);
            if (Loaded.TryGetValue(file, out var cached)) return cached;
            TMP_FontAsset asset = null;
            var font = Resources.Load<Font>(file);
            if (font == null) Debug.LogWarning("[next] 字型缺檔：" + file);
            else
            {
                try { asset = TMP_FontAsset.CreateFontAsset(font); }
                catch (System.Exception error) { Debug.LogWarning("[next] 字型建不出來：" + file + " " + error.Message); }
            }
            if (asset != null)
            {
                var fallback = Fallback();
                if (fallback != null)
                {
                    if (asset.fallbackFontAssetTable == null) asset.fallbackFontAssetTable = new List<TMP_FontAsset>();
                    asset.fallbackFontAssetTable.Add(fallback);
                }
            }
            Loaded[file] = asset;
            return asset;
        }

        /// <summary>界面語言那支。</summary>
        public static TMP_FontAsset Ui => For(NextConfig.UiLanguage);

        /// <summary>她的話、角色名：日語字型（不跟界面語言）。</summary>
        public static TMP_FontAsset Her => For(NextText.Ja);

        /// <summary>之後新建的字用界面語言那支（建頁之前叫；換語言時也叫）。</summary>
        public static void ApplyUi()
        {
            UiBuilder.UseTmpFont(Ui);
        }

        /// <summary>把某一段字改成她的字型（她的氣泡、角色名）。legacy 後端時不動。</summary>
        public static void UseHer(Component text)
        {
            var tmp = text as TMP_Text;
            var font = Her;
            if (tmp != null && font != null) tmp.font = font;
        }

        /// <summary>常駐的那幾塊（頂欄、狀態列）只建一次：換語言時把裡面的字全換成新的界面字型。</summary>
        public static void Retint(Transform root)
        {
            var font = Ui;
            if (root == null || font == null) return;
            foreach (var tmp in root.GetComponentsInChildren<TMP_Text>(true)) tmp.font = font;
        }

        private static TMP_FontAsset Fallback()
        {
            if (_fallbackTried) return _fallback;
            _fallbackTried = true;
            var font = Resources.Load<Font>("Fonts/DroidSansFallback");
            if (font == null) return null;
            try { _fallback = TMP_FontAsset.CreateFontAsset(font); }
            catch (System.Exception) { _fallback = null; }
            return _fallback;
        }
    }
}
