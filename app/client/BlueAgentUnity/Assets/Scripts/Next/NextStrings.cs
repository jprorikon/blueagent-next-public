using System.Collections.Generic;
using BlueAgent.Logic;
using UnityEngine;

namespace BlueAgent.Next
{
    /// <summary>
    /// Next 的界面文字（2026-10-28 完整 i18n）：**畫面上每一個我們自己寫的字都從這裡拿**。
    ///
    /// 字在 `Resources/i18n/next_{locale}.json`（一種語言一份扁平 JSON），查表／退回／代換在
    /// Logic 的 `NextText`（`dotnet test` 釘住）。伺服器回的內容（她說的話、桌上的敘事、角色名）不翻。
    /// `tools/check_next_strings.py` 守著：三份 key 一致、佔位符一致、`Scripts/Next` 裡不准再寫死中日文。
    /// </summary>
    public static class NextStrings
    {
        public const string Ja = NextText.Ja;
        public const string ZhHans = NextText.ZhHans;
        public const string ZhHant = NextText.ZhHant;

        private static NextText _text;

        private static NextText Text
        {
            get
            {
                if (_text != null) return _text;
                var sources = new Dictionary<string, string>();
                foreach (var locale in NextText.Locales)
                {
                    var asset = Resources.Load<TextAsset>("i18n/next_" + locale);
                    if (asset != null) sources[locale] = asset.text;
                    else Debug.LogWarning("[next] 界面文字缺檔：i18n/next_" + locale);
                }
                _text = NextText.Parse(sources);
                return _text;
            }
        }

        public static string Get(string key)
        {
            return Text.Get(key, NextConfig.UiLanguage);
        }

        public static string Get(string key, string locale)
        {
            return Text.Get(key, locale);
        }

        /// <summary>帶佔位符的那幾條：`F("history_count", "count", 3)` ⇒ `{count}` 換成 3。</summary>
        public static string F(string key, params object[] args)
        {
            return NextText.Format(Get(key), args);
        }

        public static bool Has(string key)
        {
            return Text.Has(key);
        }

        /// <summary>
        /// 伺服器給的代碼（`online`、`chat`…）→ 這一種語言的說法：表裡有 `{prefix}{code}` 就用它，沒有就原樣回代碼
        /// （新代碼不會因為客戶端還沒翻而整個不見）。
        /// </summary>
        public static string Code(string prefix, string code)
        {
            var key = prefix + (code ?? "");
            return Text.Has(key) ? Get(key) : code ?? "";
        }

        /// <summary>失敗的請求 → 一句給人看的話（伺服器錯誤碼優先，其次 HTTP 狀態）。</summary>
        public static string Error(int status, string code)
        {
            return NextText.Format(Get(Text.ErrorKey(status, code)), "status", status, "code", code ?? "");
        }

        /// <summary>串流斷掉的原文（`turn 503 {…}`／`audio transport: …`）→ 界面語言的一句話；看不懂的格式原樣回。</summary>
        public static string StreamFailure(string failure)
        {
            return NextText.TryParseStreamFailure(failure, out var status, out var code) ? Error(status, code) : failure ?? "";
        }

        /// <summary>只有錯誤碼（串流裡的 `error` 幀）：有專屬說法就用，沒有就「エラー（碼）」。</summary>
        public static string ErrorCode(string code)
        {
            var key = "err_code_" + (code ?? "");
            return Text.Has(key) ? Get(key) : F("err_unknown", "code", code ?? "");
        }

        public static string Next(string locale)
        {
            return NextText.NextLocale(locale);
        }

        public static string Label(string locale)
        {
            return NextText.Label(locale);
        }
    }
}
