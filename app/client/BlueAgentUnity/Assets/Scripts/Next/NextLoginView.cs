using BlueAgent.Screens;
using System;
using TMPro;
using UnityEngine;

namespace BlueAgent.Next
{
    /// <summary>
    /// 登入頁（兩端同一頁）：貼 token → `GET /api/me`。
    /// 契約 `accounts-and-clients.md` §2 的第一步；role 由伺服器回，端末不猜。
    ///
    /// ⚠️ **沒有位址欄位**（owner 2026-10-29）：位址寫死在 `NextConfig.DefaultBaseUrl`
    /// （Tailscale Funnel 公網入口），使用者只貼 token。
    /// </summary>
    public static class NextLoginView
    {
        public static void Build(RectTransform root, string message, Action<string> onConnect,
                                 Action<string> onPair = null, string pairCode = "")
        {
            // 可捲動（2026-11-17 模擬器實測）：鍵盤一出來，外殼把根版面下緣撐成鍵盤高度，
            // 以前這裡是固定高的直欄 ⇒ 內容比可用高度長時被硬壓，token 卡片下緣疊到配對卡、錯誤提示被蓋住。
            // 改成捲動欄：內容照自己的高度排，放不下就捲。
            var body = NextUi.ScrollArea("LoginBody", root, 12f, new RectOffset(22, 22, 30, 22), out _);
            var bodyLayout = body.GetComponent<UnityEngine.UI.VerticalLayoutGroup>();
            if (bodyLayout != null) bodyLayout.childAlignment = TextAnchor.UpperCenter;

            // 主視覺：慢慢轉的光環＋粗斜體品牌字＋天藍「NEXT」斜條（光環是原創幾何，見 bake_next_icons.py）
            var hero = UiBuilder.Column("LoginHero", body, 6f, new RectOffset(0, 0, 18, 6), true, TextAnchor.UpperCenter);
            UiBuilder.FixedHeight(hero, 150f);
            var halo = (RectTransform)UiBuilder.Icon("LoginHalo", "halo", hero, 150f,
                                                     new Color(NextTheme.Sky.r, NextTheme.Sky.g, NextTheme.Sky.b, 0.2f)).transform;
            halo.GetComponent<UnityEngine.UI.LayoutElement>().ignoreLayout = true;
            halo.anchorMin = halo.anchorMax = new Vector2(0.5f, 0.5f);
            halo.sizeDelta = new Vector2(150f, 150f);
            halo.anchoredPosition = Vector2.zero;
            halo.gameObject.AddComponent<NextSpin>().DegreesPerSecond = 12f;
            var brand = UiBuilder.Text("LoginBrand", hero, "BlueAgent", 32f, NextTheme.Ink,
                                       TextAlignmentOptions.Center, noWrap: true, bold: true);
            NextUi.Italic(brand);
            UiBuilder.FixedHeight((RectTransform)brand.transform, 44f);
            var tagRow = UiBuilder.Row("LoginTagRow", hero, 0f, null, TextAnchor.MiddleCenter);
            UiBuilder.FixedHeight(tagRow, 22f);
            NextUi.Badge("LoginTag", tagRow, "NEXT", NextTheme.Sky, NextTheme.OnColor);
            var sub = UiBuilder.Text("LoginSub", hero, NextStrings.Get("login_sub"), NextTheme.NoteSize, NextTheme.InkSoft,
                                     TextAlignmentOptions.Center, noWrap: true);
            UiBuilder.FixedHeight((RectTransform)sub.transform, 18f);

            var card = NextUi.Card("LoginCard", body, 16f, 8f);
            UiBuilder.Weight(card, 0f);

            // ⚠️ **沒有位址欄位**（owner 2026-10-29：「登入不需要手動輸入位址」）。
            //    位址寫死在 `NextConfig.DefaultBaseUrl`（Tailscale Funnel 公網入口），
            //    設定頁也不准看到它 ⇒ 使用者只貼 token。
            NextUi.Section("LoginTokenHead", card, NextStrings.Get("token"), "users");
            // token 欄＋「貼上」：token 幾十個字元不可能手打。輸入框為了畫游標藏掉了系統輸入列
            // （`UiBuilder.Input` 的 `shouldHideMobileInput`），iOS 長按的「貼上」選單就掛在那一列上 ⇒
            // 沒有這顆鈕就只能手打（2026-10-31 回報）。
            var tokenRow = UiBuilder.Row("LoginTokenRow", card, 8f, null, TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(tokenRow, 38f);
            var token = NextUi.Field("LoginToken", tokenRow, "ba_… / ot_…", 38f);
            UiBuilder.Weight((RectTransform)token.transform, 1f);
            TokenField = token;
            if (NextConfig.HasToken) token.text = NextConfig.Token;
            AsciiOnly(token, TokenChars);
            // 登入欄**不藏**系統輸入列：長按那一列的「貼上」是使用者自己點的，iOS 不會再問權限；
            // 下面那顆「貼上」鈕是程式讀剪貼簿，iOS 16 起要使用者允許（模擬器上那個詢問沒跳出來就被判成拒絕，
            // 2026-10-31 實測；設定 App 裡本 App 的「從其他 App 貼上」可改回「詢問」）。代價只是這一欄不畫自己的游標——輸入列裡看得到字（聊天欄照舊藏著）。
            token.shouldHideMobileInput = false;
            var paste = NextUi.Button("LoginPaste", tokenRow, NextStrings.Get("login_paste"), NextUi.Style.Secondary,
                                      38f, 0f, null, true, 12f);
            paste.onClick.AddListener(() => PasteToken(token));

            var connect = NextUi.Button("LoginConnect", card, NextStrings.Get("login_connect"), NextUi.Style.Accent, 42f, 0f,
                                        "chevron-right", true, 13f);
            connect.onClick.AddListener(() =>
            {
                var tokenText = NormalizeToken(token != null ? token.text : "");
                if (token != null && token.text != tokenText) token.text = tokenText;
                if (onConnect != null) onConnect(tokenText);
            });

            var state = UiBuilder.Text("LoginState", card, string.IsNullOrEmpty(message) ? "" : message,
                                       11f, string.IsNullOrEmpty(message) ? NextUi.InkSoft : NextUi.Danger,
                                       TextAlignmentOptions.TopLeft);
            StateText = state;

            // 配對碼（owner 2026-11-16：多設備配對）：已登入的端末「設定 › 端末 › 端末を追加」開的 6 桁
            if (onPair != null)
            {
                var pairCard = NextUi.Card("LoginPairCard", body, 16f, 8f);
                UiBuilder.Weight(pairCard, 0f);
                NextUi.Section("LoginPairHead", pairCard, NextStrings.Get("login_pair_head"), "phone");
                var pairRow = UiBuilder.Row("LoginPairRow", pairCard, 8f, null, TextAnchor.MiddleLeft);
                UiBuilder.FixedHeight(pairRow, 38f);
                var code = NextUi.Field("LoginPairCode", pairRow, "000000", 38f);
                UiBuilder.Weight((RectTransform)code.transform, 1f);
                code.contentType = TMP_InputField.ContentType.IntegerNumber;
                code.keyboardType = TouchScreenKeyboardType.NumberPad;
                code.characterLimit = 6;
                code.shouldHideMobileInput = false;
                if (!string.IsNullOrEmpty(pairCode)) code.text = pairCode;
                PairField = code;
                var pair = NextUi.Button("LoginPair", pairRow, NextStrings.Get("login_pair"), NextUi.Style.Secondary,
                                         38f, 0f, null, true, 12f);
                pair.onClick.AddListener(() => onPair(code != null ? code.text.Trim() : ""));
            }

            NextUi.Note("LoginHint", body, NextStrings.Get("login_hint"));

            Root = root;
            UiBuilder.Settle(root);
        }

        /// <summary>建頁時的根：狀態列換字之後要重新排版（見 <see cref="SetState"/>）。</summary>
        private static RectTransform Root;

        public static TMP_InputField TokenField { get; private set; }
        public static TMP_InputField PairField { get; private set; }
        public static Component StateText { get; private set; }

        /// <summary>
        /// token 允許的字元：`ba_&lt;48 hex&gt;`／`ot_&lt;base64url&gt;` ⇒ **英數＋`_` `-` `.`**。
        /// 刻意不含空白、假名、全形字。
        /// </summary>
        private const string TokenChars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-";

        /// <summary>
        /// **只吃 ASCII**：owner 2026-10-22 實測——手機是日語輸入法時，`adb`／手打進 token 會被轉成假名
        /// （實例：`…wpcc…` 變成 `…wpっ…`）⇒ 401。
        /// 兩道一起下：① `keyboardType = ASCIICapable`（叫出純英數鍵盤、不走 IME 組字）；
        /// ② `onValidateInput` 逐字擋掉不在白名單裡的字元（貼上也不會混進來）。
        /// </summary>
        private static void AsciiOnly(TMP_InputField field, string allowed)
        {
            if (field == null) return;
            field.keyboardType = TouchScreenKeyboardType.ASCIICapable;
            field.onValidateInput = (text, index, added) =>
                added == '\b' || added == '\n' || allowed.IndexOf(added) >= 0 ? added : '\0';
        }

        /// <summary>剪貼簿 → token 欄：只留 token 會有的字元（換行、空白、全形字一律丟掉），游標放到最後。</summary>
        private static void PasteToken(TMP_InputField field)
        {
            if (field == null) return;
            // iOS 16 起程式讀剪貼簿要使用者允許；拒絕過就讀回空字串（那時改用輸入列長按貼上）
            var clip = GUIUtility.systemCopyBuffer ?? "";
            var kept = new System.Text.StringBuilder(clip.Length);
            foreach (var ch in clip)
            {
                if (TokenChars.IndexOf(ch) >= 0) kept.Append(ch);
            }
            if (kept.Length == 0)
            {
                SetState(NextStrings.Get("login_paste_empty"), true);
                return;
            }
            field.text = kept.ToString();
            field.caretPosition = field.text.Length;
            SetState("", false);
        }

        /// <summary>
        /// token 前綴一律小寫：iOS 的系統輸入列會把句首字母自動大寫（`ot_…` 打成 `Ot_…` ⇒ 401；2026-10-31 模擬器實測）。
        /// 只動前綴：後面是 base64url／hex，大小寫有意義。
        /// </summary>
        public static string NormalizeToken(string raw)
        {
            var text = (raw ?? "").Trim();
            if (text.Length < 3) return text;
            var head = text.Substring(0, 3).ToLowerInvariant();
            return head == "ot_" || head == "ba_" || head == "gt_" ? head + text.Substring(3) : text;
        }

        /// <summary>把狀態列的字換掉（接続中…／失敗原因）。</summary>
        public static void SetState(string text, bool error)
        {
            if (StateText == null) return;
            var tmp = StateText as TMP_Text;
            if (tmp == null) return;
            var changed = tmp.text != (text ?? "");
            tmp.text = text ?? "";
            tmp.color = error ? NextUi.Danger : NextUi.InkSoft;
            // 字數變了就重排（2026-11-17 模擬器實測）：`Settle` 把文字高度**釘成建頁那一刻量到的**，
            // 之後換成三行的錯誤說明（剪貼簿讀不到那句）就只有一行高、垂直置中往上溢，壓到「接続」鈕上。
            if (changed && Root != null && tmp.transform.IsChildOf(Root)) UiBuilder.Settle(Root);
        }
    }
}
