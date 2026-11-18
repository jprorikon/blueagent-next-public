using System;
using BlueAgent.Logic;
using BlueAgent.Screens;
using TMPro;
using UnityEngine;
using Object = UnityEngine.Object;

namespace BlueAgent.Next
{
    /// <summary>
    /// 主人指令的收件箱（PENDING B.Agent.0：「Unity 收件箱 UI：看佇列、點取消」，2026-11-17）。只有主人看得到。
    ///
    /// · 交代：一個輸入框＋選角色 ⇒ `POST /api/command`（工作指令，不是閒聊；回 202＋編號，做完的結果照舊送進她的聊天）。
    /// · 一覽：`GET /api/commands` ⇒ 執行中／排隊中／最近結束的，每條有狀態、指令開頭、結果開頭。
    /// · 取消：排隊中與執行中的才有鈕 ⇒ `POST /api/command/cancel`。排隊的立刻取消；執行中的不殺，
    ///   跑完那一步就停、結果不交付（伺服器回 `cancel_requested`，這裡標「取消待ち」）。
    /// 不常駐輪詢：進頁、送出、取消之後各拉一次，另有「更新」鈕。
    /// </summary>
    public static class NextCommandsView
    {
        private static RectTransform _pane;
        private static NextShell _shell;
        private static string _entity = "";
        private static string _draft = "";
        private static string _note = "";
        private static JObject _last;

        public static void Build(RectTransform pane, NextShell shell)
        {
            _pane = UiBuilder.Column("CmdPane", pane, 0f, null, true, TextAnchor.UpperLeft);
            UiBuilder.Weight(_pane, 1f);
            _shell = shell;
            _note = "";
            if (string.IsNullOrEmpty(_entity)) _entity = shell.EntityId;
            Reload();
        }

        private static void Reload()
        {
            if (_shell == null) return;
            _shell.Api.Get("/api/commands?limit=20", reply =>
            {
                if (_shell.HandleAuth(reply)) return;
                if (!reply.Ok || reply.Json == null)
                {
                    _note = NextStrings.F("cmd_list_fail", "reason", reply.UserMessage());
                    Render();
                    return;
                }
                _last = reply.Json.OptJObject("commands");
                Render();
            });
        }

        private static void Clear()
        {
            if (_pane == null) return;
            for (var i = _pane.childCount - 1; i >= 0; i--) Object.Destroy(_pane.GetChild(i).gameObject);
        }

        private static void Render()
        {
            Clear();
            if (_pane == null) return;
            var column = UiBuilder.Column("CmdBody", _pane, 7f, new RectOffset(10, 10, 4, 10), true, TextAnchor.UpperLeft);
            UiBuilder.Weight(column, 1f);
            BuildCompose(column);

            var running = _last?.OptJArray("running");
            var queued = _last?.OptJArray("queued");
            var recent = _last?.OptJArray("recent");
            BuildList(column, "CmdNow", NextStrings.Get("cmd_now"), "pulse", running, queued, true);
            BuildList(column, "CmdDone", NextStrings.Get("cmd_recent"), "check", recent, null, false);

            if (!string.IsNullOrEmpty(_note)) NextUi.Note("CmdNote", column, _note, NextUi.Danger);
            UiBuilder.Settle((RectTransform)_pane);
        }

        private static void BuildCompose(RectTransform column)
        {
            var card = NextUi.Card("CmdCompose", column, 11f, 6f);
            UiBuilder.Weight(card, 0f);
            NextUi.Section("CmdComposeHead", card, NextStrings.Get("cmd_new"), "plus");
            NextUi.Note("CmdComposeHint", card, NextStrings.Get("cmd_hint"));

            var who = UiBuilder.Row("CmdWho", card, 6f, null, TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(who, 30f);
            var entities = _shell.Entities;
            for (var i = 0; entities != null && i < entities.Length(); i++)
            {
                var id = entities.OptJObject(i)?.OptString("id") ?? "";
                if (string.IsNullOrEmpty(id)) continue;
                var picked = id == _entity;
                var pill = NextUi.Button("CmdWho" + i, who, _shell.DisplayNameOf(id),
                                         picked ? NextUi.Style.Accent : NextUi.Style.Ghost, 28f, 0f, null, true, 11f);
                pill.onClick.AddListener(() => { _entity = id; Render(); });
            }

            var field = NextUi.Field("CmdText", card, NextStrings.Get("cmd_placeholder"), 72f);
            field.lineType = TMP_InputField.LineType.MultiLineNewline;
            field.text = _draft;
            field.onValueChanged.AddListener(value => _draft = value ?? "");

            var actions = UiBuilder.Row("CmdActions", card, 6f, null, TextAnchor.MiddleRight);
            UiBuilder.FixedHeight(actions, 36f);
            var refresh = NextUi.Button("CmdRefresh", actions, NextStrings.Get("cmd_refresh"), NextUi.Style.Ghost, 34f, 0f, "refresh");
            refresh.onClick.AddListener(() => { _note = ""; Reload(); });
            var send = NextUi.Button("CmdSend", actions, NextStrings.Get("cmd_send"), NextUi.Style.Accent, 34f, 0f, "send");
            send.onClick.AddListener(Submit);
        }

        private static void Submit()
        {
            var text = (_draft ?? "").Trim();
            if (text.Length == 0 || string.IsNullOrEmpty(_entity)) return;
            var body = new JObject().Put("text", text).Put("entity", _entity).Put("source", "phone:command");
            _shell.Api.Post("/api/command", body, reply =>
            {
                if (_shell.HandleAuth(reply)) return;
                if (!reply.Ok)
                {
                    var reason = reply.Json?.OptString("error") ?? "";
                    _note = reason == "queue_full" || reply.Status == 429
                        ? NextStrings.Get("cmd_full")
                        : NextStrings.F("cmd_send_fail", "reason", reply.UserMessage());
                    Render();
                    return;
                }
                _draft = "";
                _note = "";
                Reload();
            });
        }

        private static void BuildList(RectTransform column, string name, string title, string icon,
                                      JArray first, JArray second, bool live)
        {
            var card = NextUi.Card(name + "Card", column, 11f, 6f);
            UiBuilder.Weight(card, 0f);
            NextUi.Section(name + "Head", card, title, icon);
            var shown = 0;
            foreach (var rows in new[] { first, second })
            {
                // 最近結束的伺服器是舊到新排；畫面上新的在上面
                for (var k = 0; rows != null && k < rows.Length(); k++)
                {
                    var i = live ? k : rows.Length() - 1 - k;
                    var item = rows.OptJObject(i);
                    if (item == null) continue;
                    BuildRow(card, name + shown, item);
                    shown++;
                }
            }
            if (shown == 0) NextUi.Note(name + "Empty", card, NextStrings.Get(live ? "cmd_none_now" : "cmd_none_recent"));
        }

        private static void BuildRow(RectTransform card, string name, JObject item)
        {
            var id = item.OptInt("id");
            var state = item.OptString("state");
            var cancelAsked = item.OptBoolean("cancel_requested");
            var row = UiBuilder.Column(name, card, 3f, new RectOffset(8, 8, 6, 6), true, TextAnchor.UpperLeft);
            UiBuilder.Backdrop(name + "Bg", row, NextTheme.Field, radius: 6f);

            var top = UiBuilder.Row(name + "Top", row, 6f, null, TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(top, 18f);
            UiBuilder.Text(name + "Who", top, "#" + id + "  " + _shell.DisplayNameOf(item.OptString("entity")), 12.5f,
                           NextTheme.Ink, TextAlignmentOptions.MidlineLeft, weight: 1f, noWrap: true, bold: true);
            NextUi.Badge(name + "State", top, StateLabel(state, cancelAsked),
                         state == "failed" || state == "expired" ? NextTheme.Field : NextTheme.SkyPale,
                         state == "failed" ? NextUi.Danger : NextTheme.SkyDeep);

            UiBuilder.Text(name + "Text", row, item.OptString("preview"), 12f, NextTheme.Ink,
                           TextAlignmentOptions.TopLeft);
            var result = item.OptString("result_preview");
            if (!string.IsNullOrEmpty(result))
                NextUi.Note(name + "Result", row, NextStrings.F("cmd_result", "text", result));
            var error = item.OptString("error");
            if (!string.IsNullOrEmpty(error) && state == "failed")
                NextUi.Note(name + "Error", row, NextStrings.F("cmd_error", "reason", error), NextUi.Danger);
            NextUi.KeyValue(name + "When", row, NextStrings.Get("cmd_when"),
                            When(item.OptDouble("finished_at") > 0 ? item.OptDouble("finished_at")
                                 : item.OptDouble("started_at") > 0 ? item.OptDouble("started_at")
                                 : item.OptDouble("queued_at")));

            if ((state == "queued" || state == "running") && !cancelAsked)
            {
                var actions = UiBuilder.Row(name + "Actions", row, 6f, null, TextAnchor.MiddleRight);
                UiBuilder.FixedHeight(actions, 30f);
                var cancel = NextUi.Button(name + "Cancel", actions, NextStrings.Get("cmd_cancel"), NextUi.Style.Danger,
                                           28f, 0f, "close", true, 11f);
                cancel.onClick.AddListener(() =>
                {
                    _shell.Api.Post("/api/command/cancel", new JObject().Put("id", id), reply =>
                    {
                        if (_shell.HandleAuth(reply)) return;
                        _note = reply.Ok || reply.Status == 409 ? "" : NextStrings.F("cmd_cancel_fail", "reason", reply.UserMessage());
                        Reload();
                    });
                });
            }
        }

        private static string StateLabel(string state, bool cancelAsked)
        {
            switch (state)
            {
                case "queued": return NextStrings.Get("cmd_state_queued");
                case "running": return NextStrings.Get(cancelAsked ? "cmd_state_cancelling" : "cmd_state_running");
                case "done": return NextStrings.Get("cmd_state_done");
                case "failed": return NextStrings.Get("cmd_state_failed");
                case "cancelled": return NextStrings.Get("cmd_state_cancelled");
                case "expired": return NextStrings.Get("cmd_state_expired");
                default: return state ?? "";
            }
        }

        private static string When(double unix)
        {
            if (unix <= 0) return "—";
            return DateTimeOffset.FromUnixTimeSeconds((long)unix).ToLocalTime().ToString("MM-dd HH:mm");
        }
    }
}
