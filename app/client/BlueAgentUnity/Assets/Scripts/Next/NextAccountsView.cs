using BlueAgent.Screens;
using BlueAgent.Logic;
using TMPro;
using UnityEngine;

namespace BlueAgent.Next
{
    /// <summary>
    /// 帳號管理頁（**只給 master**）：列表／簽發 guest／輪替／撤銷。
    /// 契約 `accounts-and-clients.md` §3——四支端點，明文 token **只在簽發與輪替那一次**回。
    ///
    /// 端末規矩：token 顯示完就丟掉（不寫 PlayerPrefs、不寫 log），換頁即不可再取。
    /// </summary>
    public static class NextAccountsView
    {
        private static RectTransform _pane;
        private static NextShell _shell;
        /// <summary>
        /// 簽發／輪替回來的明文：只活在**簽發的那一次進頁**（U-5）。以前是靜態字串、進頁不清 ⇒
        /// 換頁回來、甚至登出再登入都還畫得出來。
        /// </summary>
        private static readonly OneShotSecret Issued = new OneShotSecret();
        private static string _note = "";

        public static void Build(RectTransform pane, NextShell shell)
        {
            // 自己的一格：重畫（`Clear`）只清這一格，不會把子頁的頁首（返回鍵＋標題）一起拆掉
            _pane = UiBuilder.Column("AccPane", pane, 0f, null, true, TextAnchor.UpperLeft);
            UiBuilder.Weight(_pane, 1f);
            _shell = shell;
            Issued.BeginVisit();                 // 重新進頁：上一次的明文作廢（同一次進頁的重畫走 Reload）
            if (string.IsNullOrEmpty(_note)) _note = "";
            Reload();
        }

        /// <summary>登出／回登入頁：明文一律忘掉（`NextShell.ShowLogin` 叫）。</summary>
        public static void Forget()
        {
            Issued.Forget();
        }

        private static void Clear()
        {
            if (_pane == null) return;
            for (var i = _pane.childCount - 1; i >= 0; i--) Object.Destroy(_pane.GetChild(i).gameObject);
        }

        private static void Reload()
        {
            if (_shell == null) return;
            _shell.Api.Get("/api/accounts", reply =>
            {
                if (_shell.HandleAuth(reply)) return;
                if (_shell.HandleForbidden(reply, NextStrings.Get("accounts"))) return;
                if (!reply.Ok)
                {
                    Render(null, NextStrings.F("acc_list_fail", "reason", reply.UserMessage()));
                    return;
                }
                Render(reply.Json.OptJArray("accounts"), _note);
            });
        }

        private static void Render(JArray accounts, string note)
        {
            Clear();
            if (_pane == null) return;
            var column = UiBuilder.Column("AccBody", _pane, 7f, new RectOffset(10, 10, 4, 10), true, TextAnchor.UpperLeft);
            UiBuilder.Weight(column, 1f);

            // 簽發結果（明文 token 只出現在這裡，換頁就沒了）
            if (Issued.HasValue)
            {
                var tokenCard = NextUi.Card("AccTokenCard", column, 11f, 5f);
                UiBuilder.Weight(tokenCard, 0f);
                NextUi.Section("AccTokenHead", tokenCard, NextStrings.F("acc_issued", "label", Issued.Label), "check");
                var tokenText = UiBuilder.Text("AccTokenValue", tokenCard, Issued.Value, 12f,
                                               NextTheme.SkyDeep, TextAlignmentOptions.TopLeft);
                NextUi.Note("AccTokenNote", tokenCard, NextStrings.Get("acc_token_note"));
                UiBuilder.Settle((RectTransform)_pane);
            }

            // 一覧
            var listCard = NextUi.Card("AccListCard", column, 11f, 6f);
            UiBuilder.Weight(listCard, 0f);
            var listHead = UiBuilder.Row("AccListHead", listCard, 6f, null, TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(listHead, 30f);
            UiBuilder.Icon("AccListIcon", "users", listHead, 16f, NextTheme.Sky);
            UiBuilder.Text("AccListTitle", listHead, NextStrings.Get("acc_list_title"), NextTheme.HeadSize, NextTheme.Ink,
                           TextAlignmentOptions.MidlineLeft, weight: 1f, noWrap: true, bold: true);
            var refresh = NextUi.IconButton("AccRefresh", listHead, "refresh", NextUi.Style.Ghost, 36f);
            refresh.onClick.AddListener(Reload);

            var rows = accounts;
            if (rows == null || rows.Length() == 0)
            {
                NextUi.Note("AccEmpty", listCard, NextStrings.Get("acc_empty"));
            }
            for (var i = 0; rows != null && i < rows.Length(); i++)
            {
                var account = rows.OptJObject(i);
                if (account == null) continue;
                BuildRow(listCard, account, i);
            }

            // 簽發
            var issueCard = NextUi.Card("AccIssueCard", column, 11f, 5f);
            UiBuilder.Weight(issueCard, 0f);
            NextUi.Section("AccIssueHead", issueCard, NextStrings.Get("acc_issue_head"), "plus");
            var principal = NextUi.Field("AccPrincipal", issueCard, NextStrings.Get("acc_principal_hint"), 34f);
            var display = NextUi.Field("AccDisplay", issueCard, NextStrings.Get("acc_display_hint"), 34f);
            var issue = NextUi.Button("AccIssue", issueCard, NextStrings.Get("acc_issue"), NextUi.Style.Accent, 36f, 0f, "check");
            issue.onClick.AddListener(() =>
            {
                var who = principal != null ? principal.text.Trim() : "";
                if (string.IsNullOrEmpty(who))
                {
                    _note = NextStrings.Get("acc_principal_empty");
                    Render(rows, _note);
                    return;
                }
                var body = new JObject().Put("principal", who)
                                        .Put("display", display != null && !string.IsNullOrEmpty(display.text)
                                                            ? display.text.Trim() : who);
                _shell.Api.Post("/api/accounts", body, reply =>
                {
                    if (_shell.HandleAuth(reply)) return;
                    if (_shell.HandleForbidden(reply, NextStrings.Get("acc_issue"))) return;
                    if (reply.Status != 201 || reply.Json == null)
                    {
                        _note = NextStrings.F("acc_issue_fail", "reason", reply.UserMessage());
                        Render(rows, _note);
                        return;
                    }
                    Issued.Put(reply.Json.OptString("token"), who);
                    _note = "";
                    Reload();
                });
            });

            if (!string.IsNullOrEmpty(note)) NextUi.Note("AccNote", column, note, NextUi.Danger);
            NextUi.Note("AccHint", column, NextStrings.Get("acc_hint"));
            UiBuilder.Settle((RectTransform)_pane);
        }

        private static void BuildRow(RectTransform card, JObject account, int index)
        {
            var id = account.OptString("account_id");
            var revoked = account.OptBoolean("revoked");
            var row = UiBuilder.Column("AccRow" + index, card, 3f, new RectOffset(8, 8, 6, 6),
                                       true, TextAnchor.UpperLeft);
            UiBuilder.Backdrop("AccRowBg" + index, row,
                               revoked ? NextTheme.IdlePale : NextTheme.Field,
                               radius: 6f);
            var top = UiBuilder.Row("AccRowTop" + index, row, 6f, null, TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(top, 18f);
            var name = account.OptString("display");
            if (string.IsNullOrEmpty(name)) name = account.OptString("principal");
            UiBuilder.Text("AccRowName" + index, top, name, 12.5f, NextTheme.Ink,
                           TextAlignmentOptions.MidlineLeft, weight: 1f, noWrap: true, bold: true);
            var master = account.OptString("role") == "master";
            NextUi.Badge("AccRowRole" + index, top, NextStrings.Code("badge_", account.OptString("role")),
                         master ? NextTheme.SkyPale : NextTheme.PinkPale, master ? NextTheme.SkyDeep : NextTheme.Pink);
            if (revoked) NextUi.Badge("AccRowRevoked" + index, top, NextStrings.Get("badge_revoked"),
                                      NextTheme.DangerPale, NextTheme.Danger);

            NextUi.KeyValue("AccRowWho" + index, row, NextStrings.Get("acc_id"), account.OptString("principal"));
            var capabilities = account.OptJArray("capabilities");
            var caps = "";
            for (var i = 0; capabilities != null && i < capabilities.Length(); i++)
            {
                if (i > 0) caps += ", ";
                caps += NextStrings.Code("cap_", capabilities.OptString(i, ""));
            }
            NextUi.KeyValue("AccRowCaps" + index, row, NextStrings.Get("st_can"), caps);

            var actions = UiBuilder.Row("AccRowActions" + index, row, 6f, null, TextAnchor.MiddleRight);
            UiBuilder.FixedHeight(actions, 30f);
            var rotate = NextUi.Button("AccRowRotate" + index, actions, NextStrings.Get("acc_rotate"), NextUi.Style.Secondary,
                                       28f, 0f, "refresh", true, 11f);
            rotate.onClick.AddListener(() =>
            {
                _shell.Api.Post("/api/accounts/" + id + "/rotate", null, reply =>
                {
                    if (_shell.HandleAuth(reply)) return;
                    if (_shell.HandleForbidden(reply, NextStrings.Get("acc_rotate"))) return;
                    if (!reply.Ok)
                    {
                        _note = NextStrings.F("acc_rotate_fail", "reason", reply.UserMessage());
                        Reload();
                        return;
                    }
                    Issued.Put(reply.Json.OptString("token"),
                               NextStrings.F("acc_rotated_label", "principal", account.OptString("principal")));
                    _note = "";
                    Reload();
                });
            });
            if (!revoked)
            {
                var revoke = NextUi.Button("AccRowRevoke" + index, actions, NextStrings.Get("acc_revoke"), NextUi.Style.Danger,
                                           28f, 0f, "close", true, 11f);
                revoke.onClick.AddListener(() =>
                {
                    _shell.Api.Post("/api/accounts/" + id + "/revoke", null, reply =>
                    {
                        if (_shell.HandleAuth(reply)) return;
                        if (_shell.HandleForbidden(reply, NextStrings.Get("acc_revoke"))) return;
                        _note = reply.Ok ? "" : NextStrings.F("acc_revoke_fail", "reason", reply.UserMessage());
                        Reload();
                    });
                });
            }
        }
    }
}
