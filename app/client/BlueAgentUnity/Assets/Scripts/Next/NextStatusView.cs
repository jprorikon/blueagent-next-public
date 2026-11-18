using TMPro;
using BlueAgent.Screens;
using BlueAgent.Logic;
using UnityEngine;

namespace BlueAgent.Next
{
    /// <summary>
    /// 状態頁（master）：`/api/health` 的 counts ＋ 這一端自己看到的連線資訊。
    /// 用途是**除錯與驗收**：畫面上的 role／principal 是 `/api/me` 回的，不是端末自己判的。
    /// </summary>
    public static class NextStatusView
    {
        public static void Build(RectTransform pane, NextShell shell)
        {
            var card = NextUi.Card("StatusCard", pane, 12f, 6f);
            UiBuilder.Weight(card, 0f);

            NextUi.Section("StatusHead", card, NextStrings.Get("status_device"), "pulse");
            var me = shell.Me;
            NextUi.KeyValue("StatusRole", card, NextStrings.Get("st_role"),
                            me != null ? NextStrings.Code("badge_", me.OptString("role")) : "-");
            NextUi.KeyValue("StatusPrincipal", card, NextStrings.Get("principal"), me != null ? me.OptString("principal") : "-");
            NextUi.KeyValue("StatusAccount", card, NextStrings.Get("st_account"), me != null ? me.OptString("account_id") : "-");
            NextUi.KeyValue("StatusEntity", card, NextStrings.Get("st_entity"), shell.EntityId + " (" + shell.EntityDisplayName + ")");
            // ⚠️ **不顯示伺服器位址**（owner 2026-10-29：設定裡不准看到位址）。
            NextUi.KeyValue("StatusEvents", card, NextStrings.Get("events"), NextStrings.Code("events_", shell.Api.EventsState));
            NextUi.KeyValue("StatusReconnects", card, NextStrings.Get("reconnects"), shell.Api.Reconnects.ToString());
            NextUi.KeyValue("StatusLastTurn", card, NextStrings.Get("last_turn"),
                            string.IsNullOrEmpty(NextConfig.LastTurn) ? "-" : NextConfig.LastTurn);
            NextUi.KeyValue("StatusThread", card, NextStrings.Get("thread"),
                            string.IsNullOrEmpty(shell.LastThread) ? "-" : shell.LastThread);

            var caps = "";
            var can = me != null ? me.OptJObject("can") : null;
            if (can != null)
            {
                var keys = can.Keys();
                foreach (var key in keys)
                {
                    if (!can.OptBoolean(key)) continue;
                    caps += (caps.Length == 0 ? "" : ", ") + NextStrings.Code("cap_", key);
                }
            }
            NextUi.KeyValue("StatusCaps", card, NextStrings.Get("st_can"), caps);

            var healthCard = NextUi.Card("StatusHealth", pane, 12f, 6f);
            UiBuilder.Weight(healthCard, 0f);
            NextUi.Section("StatusHealthHead", healthCard, NextStrings.Get("health_head"), "server");
            var detail = UiBuilder.Text("StatusHealthBody", healthCard, NextStrings.Get("loading"), 11f, NextTheme.Ink,
                                        TextAlignmentOptions.TopLeft);
            var refresh = NextUi.Button("StatusRefresh", healthCard, NextStrings.Get("refresh"), NextUi.Style.Secondary,
                                        32f, 0f, "refresh");

            refresh.onClick.AddListener(() => Load(shell, detail));
            Load(shell, detail);

            // 換身份（master ↔ guest）不必清 app 資料：清掉本機 token 回登入頁
            var logout = NextUi.Button("StatusLogout", pane, NextStrings.Get("logout"), NextUi.Style.Danger, 36f, 0f, "logout");
            logout.onClick.AddListener(shell.Logout);
            UiBuilder.Settle((RectTransform)pane);
        }

        private static void Load(NextShell shell, Component detail)
        {
            shell.Api.Get("/api/health", reply =>
            {
                if (detail == null) return;                           // 回來前頁面已經換掉
                if (!reply.Ok)
                {
                    Show(detail, NextStrings.F("read_fail", "reason", reply.UserMessage()));
                    return;
                }
                var counts = reply.Json.OptJObject("counts");
                var text = "api " + reply.Json.OptString("api") + "  engine " + reply.Json.OptString("engine");
                if (counts != null)
                {
                    foreach (var key in counts.Keys())
                    {
                        var child = counts.OptJObject(key);
                        if (child != null) continue;                      // 巢狀的先不展開
                        text += "\n" + key + ": " + counts.OptString(key);
                    }
                }
                Show(detail, text);
            });
        }

        /// <summary>
        /// 換字之後要重排：建頁時 `Settle` 把「讀取中…」那一行的高度釘死了，回包換成好幾行也長不高，
        /// 字就壓到卡片標題上（2026-10-30 全頁截圖抓到）。⇒ 解開釘住的高度、整頁再結算一次。
        /// </summary>
        private static void Show(Component detail, string text)
        {
            NextUi.SetText(detail, text);
            var element = detail.GetComponent<UnityEngine.UI.LayoutElement>();
            if (element != null) element.minHeight = -1f;
            var canvas = detail.GetComponentInParent<Canvas>();
            if (canvas != null) UiBuilder.Settle((RectTransform)canvas.transform);
        }
    }
}
