using System;
using BlueAgent.Logic;
using BlueAgent.Screens;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>
    /// 会话列表只渲染 App Server／SQLite 的会话摘要，不在端侧另存最后一句。
    /// 1:1 与固定 Agent 群共用同一个 NextChatView；客人 DM 不属于本轮入口。
    /// </summary>
    public sealed class NextChatListView
    {
        private NextShell _shell;
        private RectTransform _list;
        private ScrollRect _scroll;
        private int _epoch;

        public void Build(RectTransform body, NextShell shell)
        {
            _shell = shell;
            _epoch++;
            var page = UiBuilder.Column("ChatListPage", body, 10f,
                new RectOffset(12, 12, 10, 12), true, TextAnchor.UpperLeft);
            UiBuilder.Weight(page, 1f);
            var slot = NextUi.Header("ChatListHead", page, NextStrings.Get("sessions_title"),
                NextConfig.HomeIsFeed ? shell.ShowFeed : (UnityEngine.Events.UnityAction)null);
            var refresh = NextUi.IconButton("ChatListRefresh", slot, "refresh", NextUi.Style.Ghost, 36f);
            refresh.onClick.AddListener(Reload);
            _list = NextUi.ScrollArea("ChatListRows", page, 8f, new RectOffset(0, 0, 2, 8), out _scroll);
            RenderLoading();
            Reload();
        }

        private void Reload()
        {
            if (_shell == null) return;
            var epoch = _epoch;
            RefreshLocal();                          // 离线也先显示本地副本与已知角色。
            _shell.Api.ChatSync.SyncConversations((ok, error) =>
            {
                if (epoch != _epoch || _list == null) return;
                if (!ok)
                {
                    if (_shell.Api.Chat != null && _shell.Api.Chat.Conversations().Count > 0) return;
                    ClearRows();
                    NextUi.Note("ChatListError", _list,
                        NextStrings.F("sessions_load_fail", "reason", error), NextUi.Danger);
                    Settle();
                    return;
                }
                if (!_shell.IsMaster) { RefreshLocal(); return; }
                // 固定房由服务端幂等创建；失败时仍保留已经拿到的 1:1 与本地群副本。
                _shell.Api.ChatSync.SyncAgentRoom((roomOk, roomError) =>
                {
                    if (epoch != _epoch || _list == null) return;
                    RefreshLocal();
                });
            });
        }

        public void RefreshLocal()
        {
            if (_list == null || _shell?.Api.Chat == null) return;
            // 第一次升级时服务端还没有 1:1 会话；仍列出可用角色，点进去才建立 direct conversation。
            var entities = _shell.Entities;
            for (var i = 0; entities != null && i < entities.Length(); i++)
            {
                var entity = entities.OptJObject(i)?.OptString("id") ?? "";
                if (!string.IsNullOrEmpty(entity))
                    _shell.Api.Chat.EnsureConversation(entity, _shell.ChatAccountId);
            }
            var rows = _shell.Api.Chat.Conversations();
            var conversations = new JArray();
            for (var i = 0; i < rows.Count; i++) conversations.Put(rows[i]);
            Render(conversations);
        }

        private void RenderLoading()
        {
            if (_list == null) return;
            ClearRows();
            NextUi.Note("ChatListLoading", _list, NextStrings.Get("loading"));
            Settle();
        }

        private void Render(JArray conversations)
        {
            ClearRows();
            var agents = new JArray();
            var people = new JArray();
            var groups = new JArray();
            for (var i = 0; conversations != null && i < conversations.Length(); i++)
            {
                var row = conversations.OptJObject(i);
                if (row == null) continue;
                RememberMembers(row);
                if (row.OptString("kind") == "group") groups.Put(row);
                else if (_shell.IsAgent(Counterpart(row))) agents.Put(row);
                else people.Put(row);              // 主人↔客人的私聊（P-C2）：Agent 看不到
            }
            if (agents.Length() == 0 && groups.Length() == 0 && people.Length() == 0)
            {
                NextUi.Note("ChatListEmpty", _list, NextStrings.Get("sessions_empty"));
                Settle();
                return;
            }
            if (agents.Length() > 0)
            {
                NextUi.Section("ChatListAgents", _list, NextStrings.Get("sessions_agents"), "halo");
                for (var i = 0; i < agents.Length(); i++) BuildRow(agents.OptJObject(i), i, false);
            }
            if (people.Length() > 0)
            {
                NextUi.Section("ChatListPeople", _list, NextStrings.Get("sessions_people"), "chat");
                for (var i = 0; i < people.Length(); i++) BuildRow(people.OptJObject(i), i, false);
            }
            if (groups.Length() > 0)
            {
                NextUi.Section("ChatListGroups", _list, NextStrings.Get("sessions_groups"), "users");
                for (var i = 0; i < groups.Length(); i++) BuildRow(groups.OptJObject(i), i, true);
            }
            Settle();
        }

        /// <summary>1:1 的另一方（不是自己的那個 participant）。</summary>
        private string Counterpart(JObject session)
        {
            var other = session.OptString("entity");
            var participants = session.OptJArray("participants");
            for (var i = 0; participants != null && i < participants.Length(); i++)
            {
                var account = participants.OptString(i);
                if (!string.IsNullOrEmpty(account) && account != _shell.ChatAccountId) other = account;
            }
            return other;
        }

        /// <summary>伺服器的 members（id／kind／display）：認得出誰是人、叫什麼。</summary>
        private void RememberMembers(JObject session)
        {
            var members = session.OptJArray("members");
            for (var i = 0; members != null && i < members.Length(); i++)
            {
                var member = members.OptJObject(i);
                if (member != null)
                    _shell.RememberPerson(member.OptString("id"), member.OptString("kind"), member.OptString("display"));
            }
        }

        private void BuildRow(JObject session, int index, bool group)
        {
            if (session == null) return;
            var entity = group ? ChatRepository.GroupKey(session.OptString("id")) : session.OptString("entity");
            if (!group)
            {
                var participants = session.OptJArray("participants");
                for (var i = 0; participants != null && i < participants.Length(); i++)
                {
                    var account = participants.OptString(i);
                    if (!string.IsNullOrEmpty(account) && account != _shell.ChatAccountId) entity = account;
                }
            }
            if (string.IsNullOrEmpty(entity)) return;
            // 1:1 保留原节点名（既有 UI 验收依赖）；群使用独立命名空间。
            var suffix = group ? "Group_" + index : entity;
            var row = UiBuilder.Row("SessionRow_" + suffix, _list, 10f,
                new RectOffset(12, 12, 8, 8), TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(row, 76f);
            var hit = row.gameObject.AddComponent<Image>();
            hit.color = new Color(1f, 1f, 1f, 0.001f);
            UiBuilder.Backdrop("SessionRowBg_" + suffix, row, NextTheme.Panel, radius: NextTheme.Radius);
            UiBuilder.Border(row, NextTheme.PanelLine, 1f, NextTheme.Radius);
            if (group) BuildGroupAvatar(session, row, suffix);
            else NextBoardUi.Face("SessionAvatar_" + suffix, row, _shell, entity, 48f, NextTheme.Sky);

            var center = UiBuilder.Column("SessionCenter_" + suffix, row, 3f, null, true, TextAnchor.MiddleLeft);
            var centerLayout = UiBuilder.Weight(center, 1f);
            centerLayout.minWidth = 0f;
            centerLayout.preferredWidth = 0f;
            var backendName = session.OptString("name");
            if (!group && string.IsNullOrEmpty(backendName))
                backendName = _shell.IsAgent(entity) ? _shell.EntityNameOf(entity) : _shell.DisplayNameOf(entity);
            var name = group
                ? (string.IsNullOrEmpty(backendName) ? NextStrings.Get("agent_room") : backendName)
                : _shell.DisplayNameOf(entity);
            Ellipsis(UiBuilder.Text("SessionName_" + suffix, center, name,
                NextTheme.BodySize, NextTheme.Ink, TextAlignmentOptions.MidlineLeft,
                noWrap: true, bold: true));
            var lastMessage = session.OptJObject("last_message");
            var last = Preview(LastText(lastMessage));
            // 群組那一行要看得出是誰說的（設計稿：「ミラ：じゃあ私から…」）；自己說的不加
            var speaker = lastMessage?.OptJObject("author")?.OptString("id") ?? "";
            if (group && !string.IsNullOrEmpty(last) && !string.IsNullOrEmpty(speaker) &&
                speaker != _shell.ChatAccountId)
                last = Preview(NextStrings.F("preview_author", "name", _shell.DisplayNameOf(speaker), "text", last));
            if (string.IsNullOrEmpty(last)) last = Preview(session.OptString("last"));
            if (string.IsNullOrEmpty(last)) last = NextStrings.Get("session_no_messages");
            // 單行＋省略號：不然長句會畫出中欄、壓到右邊的時間／箭頭與卡片邊框（2026-11-07 模擬器實測）。
            Ellipsis(UiBuilder.Text("SessionLast_" + suffix, center, last, NextTheme.NoteSize,
                NextTheme.InkSoft, TextAlignmentOptions.MidlineLeft, noWrap: true));

            var right = UiBuilder.Column("SessionRight_" + suffix, row, 5f, null, false, TextAnchor.MiddleRight);
            UiBuilder.FixedWidth(right, 52f);
            // 右欄各項按自己的寬靠右；預設會把 15pt 的箭頭橫向拉滿 52pt。
            right.GetComponent<VerticalLayoutGroup>().childForceExpandWidth = false;
            var at = lastMessage != null ? lastMessage.OptDouble("created_at") : session.OptDouble("at");
            UiBuilder.Text("SessionTime_" + suffix, right, FormatTime(at),
                NextTheme.NoteSize, NextTheme.InkFaint, TextAlignmentOptions.MidlineRight, noWrap: true);
            // 群：服务端摘要的读游标未读＋事件累加（ChatRepository.ApplyEvent），打开群即 MarkRead 清零。
            var localUnread = group ? 0 : (_shell.Chat != null ? _shell.Chat.UnreadFor(entity) : 0);
            var unread = Math.Max(localUnread, session.OptInt("unread"));
            if (unread > 0)
                NextUi.Badge("SessionUnread_" + suffix, right, unread.ToString(),
                    NextTheme.PinkPale, NextTheme.Pink);
            else
                UiBuilder.Icon("SessionGo_" + suffix, "chevron-right", right, 15f, NextTheme.InkFaint);

            var button = row.gameObject.AddComponent<Button>();
            button.targetGraphic = hit;
            button.transition = Selectable.Transition.None;
            button.onClick.AddListener(() =>
            {
                if (group) _shell.SwitchGroup(entity, name);
                else _shell.SwitchEntity(entity, backendName);
            });
            row.gameObject.AddComponent<NextPress>();
        }

        private void BuildGroupAvatar(JObject session, RectTransform row, string suffix)
        {
            var stack = UiBuilder.Row("SessionAvatarStack_" + suffix, row, -10f, null, TextAnchor.MiddleLeft);
            UiBuilder.FixedWidth(stack, 64f);
            var participants = session.OptJArray("participants");
            var shown = 0;
            for (var i = 0; participants != null && i < participants.Length() && shown < 3; i++)
            {
                var account = participants.OptString(i);
                if (string.IsNullOrEmpty(account) || account == _shell.ChatAccountId) continue;
                NextUi.Avatar("SessionGroupFace_" + suffix + "_" + shown, stack, account, 28f, NextTheme.Sky);
                shown++;
            }
            if (shown == 0) UiBuilder.Icon("SessionGroupFallback_" + suffix, "users", stack, 34f, NextTheme.Sky);
        }

        /// <summary>
        /// 單行過長收成「…」。⚠️ 行盒（字級×比例）比 CJK 字型的上下伸部矮，TMP 的 Ellipsis 也會做垂直檢查
        /// ⇒ 直接開會把第一行整條判成溢出、名字和最後一句全被吃掉（2026-11-07 模擬器實測）。
        /// 上下各給半個字級的負邊距：只放寬 TMP 的排字框，版面高度不變，水平方向照樣截斷。
        /// </summary>
        private static void Ellipsis(Component text)
        {
            if (!(text is TMP_Text tmp)) return;
            var slack = tmp.fontSize * 0.5f;
            tmp.margin = new Vector4(0f, -slack, 0f, -slack);
            tmp.overflowMode = TextOverflowModes.Ellipsis;
        }

        private static string LastText(JObject message)
        {
            if (message != null && message.OptBoolean("deleted")) return NextStrings.Get("message_deleted");
            var parts = message?.OptJArray("parts");
            var texts = new System.Collections.Generic.List<string>();
            for (var i = 0; parts != null && i < parts.Length(); i++)
            {
                var part = parts.OptJObject(i);
                if (part == null) continue;
                var text = part.OptString("text");
                if (!string.IsNullOrEmpty(text)) texts.Add(text);
                else if (part.OptString("kind") == "sticker") texts.Add(NextStrings.Get("preview_sticker"));
                else if (part.OptString("kind") == "photo") texts.Add(NextStrings.Get("preview_photo"));
                else if (part.OptString("kind") == "call") texts.Add(NextStrings.Get("chat_missed_call"));
            }
            return string.Join(" ", texts.ToArray());
        }

        private static string Preview(string text)
        {
            var flat = (text ?? "").Replace('\r', ' ').Replace('\n', ' ').Trim();
            return flat.Length <= 72 ? flat : flat.Substring(0, 72) + "…";
        }

        private static string FormatTime(double at)
        {
            if (at <= 0) return "";
            try
            {
                var local = DateTimeOffset.FromUnixTimeSeconds((long)at).LocalDateTime;
                return local.Date == DateTime.Now.Date ? local.ToString("HH:mm") : local.ToString("M/d");
            }
            catch (ArgumentOutOfRangeException)
            {
                return "";
            }
        }

        private void ClearRows()
        {
            if (_list == null) return;
            for (var i = _list.childCount - 1; i >= 0; i--)
                UnityEngine.Object.Destroy(_list.GetChild(i).gameObject);
        }

        private void Settle()
        {
            if (_list == null) return;
            UiBuilder.Settle(_list);
            if (_scroll != null) _scroll.verticalNormalizedPosition = 1f;
        }

#if BA_SELFTEST
        /// <summary>定向验收仍走正式 Render；只替换这一回包，不另画列表。</summary>
        public void SelfTestRender(JObject reply)
        {
            if (_list == null) return;
            _epoch++;                            // 让 Build 同时发出的网络回包不能覆盖验收资料。
            var rows = new JArray();
            var agents = reply?.OptJArray("agents");
            var groups = reply?.OptJArray("groups");
            for (var i = 0; agents != null && i < agents.Length(); i++) rows.Put(agents.OptJObject(i));
            for (var i = 0; groups != null && i < groups.Length(); i++) rows.Put(groups.OptJObject(i));
            Render(rows);
        }
#endif
    }
}
