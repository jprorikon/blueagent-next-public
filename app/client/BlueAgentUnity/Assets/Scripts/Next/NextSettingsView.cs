using System.Collections.Generic;
using BlueAgent.Logic;
using BlueAgent.Screens;
using UnityEngine;

namespace BlueAgent.Next
{
    /// <summary>
    /// 設定（P2）：四頁——音声／背景／サービス／診断，下面固定「帳號管理」「登出」兩列。
    ///
    /// owner 的规矩（2026-10-22）都照做：
    ///   * **不放切換角色**（角色用首页的头像列）；**不放会话／工作模式切换**；不要☰ menu。
    ///   * 首页右上角只有这一颗「設定」。
    ///   * 「没实现的开关不做假」：收音源／聞き手 这些要等本机能力接上（P5）再出现，
    ///     现在这一页只放**真的会生效**的：声 ON/OFF、音量、界面语言、背景、同步、账号、状态、登出。
    /// 2026-10-29 改成 原創幾何風格：頁首＋帶圖示的斜切頁籤、開關、音量格、語言分段選擇、清單列。
    /// </summary>
    public static class NextSettingsView
    {
        private const string PageAudio = "audio";
        private const string PageMood = "mood";
        private const string PageService = "service";
        private const string PageDiag = "diag";
        private const string PageBg = "bg";

        public static void Build(RectTransform body, NextShell shell, bool withBack)
        {
            var column = UiBuilder.Column("SettingsBody", body, 10f, new RectOffset(12, 12, 10, 12), true, TextAnchor.UpperLeft);
            UiBuilder.Weight(column, 1f);

            // 一律有「返回」：master 的設定也是蓋在聊天頁上的（owner：首頁右上那顆設定進去，要能回來）
            NextUi.Header("SettingsHead", column, NextStrings.Get("settings"), shell.CloseSettings);

            var page = string.IsNullOrEmpty(shell.SettingsPage) ? PageAudio : shell.SettingsPage;
            NextUi.Tabs("SetTab", column, new List<(string, string, string)>
            {
                (PageAudio, NextStrings.Get("audio"), "volume"),
                (PageMood, NextStrings.Get("mood_settings"), "smiley"),
                (PageBg, NextStrings.Get("background"), "landscape"),
                (PageService, NextStrings.Get("service"), "server"),
                (PageDiag, NextStrings.Get("diag"), "pulse"),
            }, page, shell.ShowSettingsPage);

            switch (page)
            {
                case PageMood: BuildMood(column, shell); break;
                case PageBg: BuildBackground(column, shell); break;
                case PageService: BuildService(column, shell); break;
                case PageDiag: BuildDiag(column, shell); break;
                default: BuildAudio(column, shell); break;
            }

            // 子頁與登出（跟頁籤無關，固定在下面）
            var links = NextUi.Card("SetLinks", column, 6f, 0f);
            // 相冊牆（伺服器上送過的圖）：聊天頁「+」只留送圖，看圖從這裡進（2026-10-29 owner）
            NextUi.ListRow("SetOpenAlbum", links, "grid", NextStrings.Get("album_title"), shell.ShowAlbum);
            NextUi.Divider("SetLinksAlbumLine", links);
            if (shell.IsMaster)
            {
                NextUi.ListRow("SetOpenAccounts", links, "users", NextStrings.Get("accounts"),
                               () => shell.ShowSubPage(NextStrings.Get("accounts"), pane => NextAccountsView.Build(pane, shell)));
                NextUi.Divider("SetLinksLine", links);
                // 主人指令的收件箱（PENDING B.Agent.0，2026-11-17）：看佇列、交代、取消
                NextUi.ListRow("SetOpenCommands", links, "history", NextStrings.Get("commands"),
                               () => shell.ShowSubPage(NextStrings.Get("commands"), pane => NextCommandsView.Build(pane, shell)));
                NextUi.Divider("SetLinksCommandsLine", links);
            }
            // 端末（多設備配對，owner 2026-11-16）：主人與客人都可以加第二台、解除丟掉的那台
            NextUi.ListRow("SetOpenDevices", links, "phone", NextStrings.Get("devices"),
                           () => shell.ShowSubPage(NextStrings.Get("devices"), pane => NextDevicesView.Build(pane, shell)));
            NextUi.Divider("SetLinksDevicesLine", links);
            NextUi.ListRow("SetLogout", links, "logout", NextStrings.Get("logout"), shell.Logout, danger: true);

            NextMotion.Stagger(column, 2);
            UiBuilder.Settle((RectTransform)body);
        }

        private static RectTransform LabeledRow(string name, RectTransform card, string label)
        {
            var row = UiBuilder.Row(name, card, 10f, null, TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(row, 30f);
            var text = UiBuilder.Text(name + "Label", row, label, NextTheme.LabelSize, NextTheme.Ink,
                                      TMPro.TextAlignmentOptions.MidlineLeft, noWrap: true);
            UiBuilder.FixedWidth((RectTransform)text.transform, 104f);
            return row;
        }

        // ── 音声 ────────────────────────────────────────────────────────────────
        private static void BuildAudio(RectTransform column, NextShell shell)
        {
            var card = NextUi.Card("SetAudio", column);
            NextUi.Section("SetAudioHead", card, NextStrings.Get("audio"), "volume");

            if (shell.IsMaster)
            {
                var on = NextConfig.WantVoice;
                var row = UiBuilder.Row("VoiceRow", card, 10f, null, TextAnchor.MiddleLeft);
                UiBuilder.FixedHeight(row, 30f);
                UiBuilder.Text("VoiceLabel", row, NextStrings.Get(on ? "voice_on" : "voice_off"), NextTheme.LabelSize,
                               NextTheme.Ink, TMPro.TextAlignmentOptions.MidlineLeft, weight: 1f, noWrap: true);
                NextUi.Toggle("VoiceToggle", row, on, value =>
                {
                    NextConfig.WantVoice = value;
                    shell.ShowSettingsPage(PageAudio);
                });
            }
            else
            {
                NextUi.Note("VoiceGuest", card, NextStrings.Get("voice_guest"));
            }
            NextUi.Divider("SetAudioLine1", card);

            // 音量：五格（點哪格就到哪格；UGUI 的 slider 手勢要自己寫，五格夠用且好按）
            var level = Mathf.RoundToInt(NextConfig.Volume * 5f);
            var vol = LabeledRow("VolRow", card, NextStrings.Get("volume"));
            var meterHost = UiBuilder.Row("VolMeterHost", vol, 6f, null, TextAnchor.MiddleLeft);
            UiBuilder.Weight(meterHost, 1f);
            NextUi.Meter("VolMeter", meterHost, level, 5, value => shell.NudgeVolume(value * 0.2f - NextConfig.Volume));
            var value = UiBuilder.Text("VolValue", vol, Mathf.RoundToInt(NextConfig.Volume * 100f) + "%", NextTheme.LabelSize,
                                       NextTheme.SkyDeep, TMPro.TextAlignmentOptions.MidlineRight, noWrap: true, bold: true);
            UiBuilder.FixedWidth((RectTransform)value.transform, 44f);
            NextUi.Divider("SetAudioLine2", card);

            // 界面語言：三格直接選（每一格用它自己的寫法標名字）
            var langHead = UiBuilder.Row("LangHead", card, 6f, null, TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(langHead, 20f);
            UiBuilder.Icon("LangIcon", "globe", langHead, 14f, NextTheme.Sky);
            UiBuilder.Text("LangLabel", langHead, NextStrings.Get("language"), NextTheme.LabelSize, NextTheme.Ink,
                           TMPro.TextAlignmentOptions.MidlineLeft, weight: 1f, noWrap: true);
            var locales = NextText.Locales;
            var labels = new List<string>();
            foreach (var locale in locales) labels.Add(NextText.Label(locale));
            NextUi.Segments("Lang", card, labels, System.Array.IndexOf(locales, NextConfig.UiLanguage), index =>
            {
                shell.ApplyLanguage(locales[index]);
                shell.ShowSettingsPage(PageAudio);
            });
            NextUi.Note("LangHint", card, NextStrings.Get("lang_hint"));
        }

        // ── 公開心情（主人與客人都是設定自己的帳號）────────────────────────────
        private static void BuildMood(RectTransform column, NextShell shell)
        {
            var card = NextUi.Card("SetMood", column);
            NextUi.Section("SetMoodHead", card, NextStrings.Get("mood_settings"), "smiley");
            NextUi.Note("SetMoodHint", card, NextStrings.Get("mood_settings_hint"));
            var current = shell.Api.Social?.Mood(shell.ChatAccountId);
            var selected = current == null ? "" : current.OptString("key");
            var note = current == null ? "" : current.OptString("note").Trim();
            if (note.Length > 0) NextUi.Note("SetMoodPublicNote", card, note);

            var keys = new[] { "calm", "happy", "excited", "irritated", "down", "lonely" };
            for (var i = 0; i < keys.Length; i += 2)
            {
                var row = UiBuilder.Row("MoodRow" + i, card, 6f, new RectOffset(4, 4, 0, 0), TextAnchor.MiddleLeft);
                UiBuilder.FixedHeight(row, 36f);
                for (var j = i; j < i + 2 && j < keys.Length; j++)
                {
                    var key = keys[j];
                    var button = NextUi.Button("Mood_" + key, row, NextStrings.Get("mood_" + key),
                        key == selected ? NextUi.Style.Navy : NextUi.Style.Secondary, 34f, 0f,
                        key == selected ? "check" : null, true, 12f);
                    UiBuilder.EqualWidth((RectTransform)button.transform);
                    button.interactable = !shell.MoodSettingBusy;
                    button.onClick.AddListener(() => shell.SetMyMood(key));
                }
            }
            if (!string.IsNullOrEmpty(shell.MoodSettingMessage))
                NextUi.Note("SetMoodFeedback", card, shell.MoodSettingMessage);
        }

        // ── 背景（P3）────────────────────────────────────────────────────────────
        private static void BuildBackground(RectTransform column, NextShell shell)
        {
            var card = NextUi.Card("SetBg", column);
            NextUi.Section("SetBgHead", card, NextStrings.Get("background"), "landscape");
            NextUi.Note("SetBgNow", card, NextStrings.F("bg_now", "name", DescribeBackground(shell.EntityId)));

            var current = NextConfig.BackgroundFor(shell.EntityId);
            var choices = new List<(string id, string label)> { ("", NextStrings.Get("none")), ("*", NextStrings.Get("bg_random")) };
            foreach (var id in BackgroundIds()) choices.Add((id, BackgroundName(id)));
            for (var i = 0; i < choices.Count; i += 2)
            {
                var row = UiBuilder.Row("BgRow" + i, card, 6f, new RectOffset(4, 4, 0, 0), TextAnchor.MiddleLeft);
                UiBuilder.FixedHeight(row, 34f);
                for (var j = i; j < i + 2 && j < choices.Count; j++)
                {
                    var (id, label) = choices[j];
                    var selected = id == current;
                    var button = NextUi.Button("Bg_" + (id.Length == 0 ? "none" : id == "*" ? "random" : id), row, label,
                                               selected ? NextUi.Style.Navy : NextUi.Style.Secondary, 32f, 0f,
                                               selected ? "check" : null, true, 11.5f);
                    UiBuilder.EqualWidth((RectTransform)button.transform);
                    var target = id;
                    if (target == "*") button.onClick.AddListener(shell.RandomBackground);
                    else button.onClick.AddListener(() => shell.SetBackground(target));
                }
            }
            NextUi.Note("SetBgHint", card, NextStrings.Get("bg_hint"));
            NextUi.Divider("SetBgLine", card);

            // 3D 待機的微動作（MAGIC）：關掉退回固定的正弦擺動
            var idle = NextConfig.IdleMotion;
            var idleRow = UiBuilder.Row("IdleMotionRow", card, 10f, null, TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(idleRow, 30f);
            UiBuilder.Text("IdleMotionLabel", idleRow, NextStrings.Get("idle_motion"), NextTheme.LabelSize,
                           NextTheme.Ink, TMPro.TextAlignmentOptions.MidlineLeft, weight: 1f, noWrap: true);
            NextUi.Toggle("IdleMotionToggle", idleRow, idle, value =>
            {
                NextConfig.IdleMotion = value;
                shell.ShowSettingsPage(PageBg);
            });
            NextUi.Note("IdleMotionHint", card, NextStrings.Get("idle_motion_hint"));
            if (idle)
            {
                // 待機用哪個模型（owner 2026-10-31）：VAR＝平滑；AR-HMM＝會換姿勢、偶爾跳一下
                var arRow = UiBuilder.Row("IdleArHmmRow", card, 10f, null, TextAnchor.MiddleLeft);
                UiBuilder.FixedHeight(arRow, 30f);
                UiBuilder.Text("IdleArHmmLabel", arRow, NextStrings.Get("idle_arhmm"), NextTheme.LabelSize,
                               NextTheme.Ink, TMPro.TextAlignmentOptions.MidlineLeft, weight: 1f, noWrap: true);
                NextUi.Toggle("IdleArHmmToggle", arRow, NextConfig.IdleArHmm, value =>
                {
                    NextConfig.IdleArHmm = value;
                    shell.ShowSettingsPage(PageBg);
                });
                NextUi.Note("IdleArHmmHint", card, NextStrings.Get("idle_arhmm_hint"));
            }
        }

        private static readonly string[] BgIds = { "school", "office", "street", "aster" };

        private static string[] BackgroundIds() { return BgIds; }

        /// <summary>背景名：界面語言的那一條（`bg_{id}`）；表裡沒有的新背景才退回場景包自己的 `name`。</summary>
        private static string BackgroundName(string id)
        {
            if (NextStrings.Has("bg_" + id)) return NextStrings.Get("bg_" + id);
            var card = Resources.Load<TextAsset>("backgrounds/" + id + "/background");
            if (card == null) return id;
            var name = new JObject(card.text).OptString("name");
            return string.IsNullOrEmpty(name) ? id : name;
        }

        private static string DescribeBackground(string entity)
        {
            var id = NextConfig.BackgroundFor(entity);
            return string.IsNullOrEmpty(id) ? NextStrings.Get("none") : BackgroundName(id);
        }

        // ── サービス ────────────────────────────────────────────────────────────
        private static void BuildService(RectTransform column, NextShell shell)
        {
            var card = NextUi.Card("SetService", column);
            NextUi.Section("SetServiceHead", card, NextStrings.Get("service"), "server");
            // ⚠️ **不顯示伺服器位址**（owner 2026-10-29）。位址寫死在客戶端，沒有「更換連線」這回事。
            NextUi.KeyValue("SrvPrincipal", card, NextStrings.Get("principal"),
                            NextStrings.F("principal_role", "principal", shell.Principal,
                                          "role", NextStrings.Get(shell.IsMaster ? "badge_master" : "badge_guest")));
            NextUi.KeyValue("SrvToken", card, NextStrings.Get("token"), Mask(NextConfig.Token));

            var counts = shell.HealthCounts;
            if (counts != null)
            {
                NextUi.KeyValue("SrvEngine", card, NextStrings.Get("engine"), counts.OptString("engine"));
                var facts = counts.OptJObject("counts");
                if (facts != null)
                {
                    NextUi.KeyValue("SrvThreads", card, NextStrings.Get("threads"), facts.OptInt("threads").ToString());
                    NextUi.KeyValue("SrvFacts", card, NextStrings.Get("facts"), facts.OptInt("visible_facts").ToString());
                }
            }
            NextUi.Divider("SrvLine", card);

            var location = NextUi.Card("SetLocation", column);
            NextUi.Section("SetLocationHead", location, NextStrings.Get("location_sharing"), "globe");
            var locationRow = UiBuilder.Row("LocationSharingRow", location, 10f, null, TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(locationRow, 32f);
            UiBuilder.Text("LocationSharingLabel", locationRow, NextStrings.Get("location_sharing"),
                           NextTheme.LabelSize, NextTheme.Ink, TMPro.TextAlignmentOptions.MidlineLeft,
                           weight: 1f, noWrap: true);
            var toggle = NextUi.Toggle("LocationSharingToggle", locationRow, shell.LocationSharing,
                                       shell.SetLocationSharing);
            toggle.interactable = !shell.LocationSharingBusy;
            NextUi.Note("LocationSharingHint", location, NextStrings.Get("location_sharing_hint"));
            if (!string.IsNullOrEmpty(shell.LocationSharingMessage))
                NextUi.Note("LocationSharingFeedback", location, shell.LocationSharingMessage);
            NextUi.Divider("LocationRefreshLine", location);
            NextUi.ListRow("LocationRefresh", location, "refresh", NextStrings.Get("refresh"),
                           shell.RefreshLocationSharing);
        }

        // ── 診断 ────────────────────────────────────────────────────────────────
        private static void BuildDiag(RectTransform column, NextShell shell)
        {
            var card = NextUi.Card("SetDiag", column);
            NextUi.Section("SetDiagHead", card, NextStrings.Get("diag"), "pulse");
            NextUi.KeyValue("DiagEvents", card, NextStrings.Get("events"), NextStrings.Code("events_", shell.Api.EventsState));
            NextUi.KeyValue("DiagReconnects", card, NextStrings.Get("reconnects"), shell.Api.Reconnects.ToString());
            NextUi.KeyValue("DiagTurn", card, NextStrings.Get("last_turn"),
                            string.IsNullOrEmpty(NextConfig.LastTurnFor(shell.EntityId)) ? "-" : NextConfig.LastTurnFor(shell.EntityId));
            NextUi.KeyValue("DiagThread", card, NextStrings.Get("thread"),
                            string.IsNullOrEmpty(shell.LastThread) ? "-" : shell.LastThread);
            NextUi.Divider("DiagLine", card);

            var row = UiBuilder.Row("DiagActions", card, 8f, new RectOffset(4, 4, 0, 0), TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(row, 36f);
            var sync = NextUi.Button("DiagSync", row, NextStrings.Get("sync_now"), NextUi.Style.Primary, 34f, 0f, "refresh");
            UiBuilder.EqualWidth((RectTransform)sync.transform);
            sync.onClick.AddListener(() =>
            {
                if (shell.Chat != null) shell.Chat.SyncMissed();
                shell.ResumeSync();
                shell.ShowSettingsPage(PageDiag);
            });
            var status = NextUi.Button("DiagStatus", row, NextStrings.Get("status_page"), NextUi.Style.Secondary, 34f, 0f, "pulse");
            UiBuilder.EqualWidth((RectTransform)status.transform);
            status.onClick.AddListener(() => shell.ShowSubPage(NextStrings.Get("status_page"),
                                                               pane => NextStatusView.Build(pane, shell)));
        }

        private static string Mask(string token)
        {
            if (string.IsNullOrEmpty(token)) return NextStrings.Get("token_none");
            if (token.Length <= 12) return token.Substring(0, 2) + "…";
            return token.Substring(0, 6) + "…" + token.Substring(token.Length - 4);
        }
    }
}
