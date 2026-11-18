using System;
using BlueAgent.Logic;
using BlueAgent.Screens;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>公共區共用的帖子零件；聊天頁的泡泡、輸入列與會話狀態不在這裡。</summary>
    public static class NextBoardUi
    {
        /// <summary>主人的頭像（owner 2026-11-08 指定的那張貓）；名字走 i18n `owner_name`（オーナー／老师）。</summary>
        public const string OwnerAvatar = "owner";

        public static bool IsOwner(JObject post)
        {
            return post != null && post.OptString("author_kind") == "owner";
        }

        public static string DisplayName(NextShell shell, JObject post)
        {
            var id = post != null ? post.OptString("author_id") : "";
            if (post != null && post.OptString("author_kind") == "entity") return shell.DisplayNameOf(id);
            // 主人在服務端的 display 是帳號名（`owner`）⇒ 畫面一律叫「オーナー／老师」（owner 2026-11-08）
            if (IsOwner(post)) return NextStrings.Get("owner_name");
            var shown = post != null ? post.OptString("display") : "";
            return string.IsNullOrEmpty(shown) ? id : shown;
        }

        public static string AvatarId(JObject post)
        {
            return post != null && post.OptString("author_kind") == "entity"
                ? post.OptString("author_id") : "_fallback";
        }

        /// <summary>設計稿是「12分前」：一小時內按分、今天按小時，其餘才寫日期。</summary>
        public static string Time(double at)
        {
            if (at <= 0) return "";
            try
            {
                var local = DateTimeOffset.FromUnixTimeSeconds((long)at).LocalDateTime;
                var ago = DateTime.Now - local;
                if (ago.TotalSeconds >= 0 && ago.TotalMinutes < 1) return NextStrings.Get("time_just_now");
                if (ago.TotalSeconds >= 0 && ago.TotalMinutes < 60)
                    return NextStrings.F("time_minutes_ago", "count", (int)ago.TotalMinutes);
                if (ago.TotalSeconds >= 0 && local.Date == DateTime.Now.Date)
                    return NextStrings.F("time_hours_ago", "count", (int)ago.TotalHours);
                return local.Date == DateTime.Now.Date
                    ? local.ToString("HH:mm") : local.ToString("M/d HH:mm");
            }
            catch (ArgumentOutOfRangeException)
            {
                return "";
            }
        }

        /// <summary>
        /// 角色的帖子是她活動收尾的原文，常帶 Markdown（`### 見出し`、`**強調**`、表格）；卡片不渲染 Markdown，
        /// 原樣顯示就是一串 `###…|…|`（2026-11-07 上線後實測）。這裡只做輕量去殼：去掉標題井號與粗體記號、
        /// 表格列改成「a / b / c」、表格分隔線整行拿掉。人寫的投稿也走同一支（多半沒有這些記號，等於不變）。
        /// </summary>
        public static string Plain(string text)
        {
            var lines = (text ?? "").Replace("\r\n", "\n").Split('\n');
            var kept = new System.Collections.Generic.List<string>();
            foreach (var raw in lines)
            {
                var line = raw.Trim();
                if (line.Length == 0) { kept.Add(""); continue; }
                var bare = line.Replace("|", "").Replace("-", "").Replace(":", "").Trim();
                if (line.Contains("|") && bare.Length == 0) continue;            // |---|---|
                line = System.Text.RegularExpressions.Regex.Replace(line, @"^#{1,6}\s*", "");
                line = line.Replace("**", "").Replace("__", "").Replace("`", "");
                if (line.Contains("|"))
                {
                    var cells = new System.Collections.Generic.List<string>();
                    foreach (var cell in line.Split('|'))
                    {
                        // 摘要把表格壓成一行時，分隔列的「---」會混在內容裡 ⇒ 只有 -／: 的格子一律丟
                        var trimmed = cell.Trim();
                        if (trimmed.Length > 0 && trimmed.Trim('-', ':', ' ').Length > 0) cells.Add(trimmed);
                    }
                    line = string.Join(" / ", cells.ToArray());
                }
                kept.Add(line);
            }
            return string.Join("\n", kept.ToArray()).Trim();
        }

        public static bool CanDelete(NextShell shell, JObject post)
        {
            if (shell.IsMaster) return true;
            return post != null && post.OptString("author_id") == shell.Principal;
        }

        public static RectTransform Heading(string name, RectTransform parent, NextShell shell, JObject post)
        {
            var row = UiBuilder.Row(name, parent, 8f, null, TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(row, 42f);
            RectTransform avatar;
            if (post != null && post.OptString("author_kind") == "entity")
                avatar = NextUi.Avatar(name + "Avatar", row, AvatarId(post), 38f, NextTheme.Sky);
            else if (IsOwner(post))
                avatar = NextUi.Avatar(name + "Avatar", row, OwnerAvatar, 38f, NextTheme.Sky);
            else
                avatar = PersonAvatar(name + "Avatar", row, DisplayName(shell, post), 38f);
            OpensProfile(avatar, shell, post);
            var names = UiBuilder.Column(name + "Names", row, 1f, null, true, TextAnchor.MiddleLeft);
            var layout = UiBuilder.Weight(names, 1f);
            layout.minWidth = 0f;
            layout.preferredWidth = 0f;
            UiBuilder.Text(name + "Display", names, DisplayName(shell, post), NextTheme.BodySize,
                NextTheme.Ink, TextAlignmentOptions.MidlineLeft, noWrap: true, bold: true);
            UiBuilder.Text(name + "Time", names, Time(post.OptDouble("at")), NextTheme.NoteSize,
                NextTheme.InkSoft, TextAlignmentOptions.MidlineLeft, noWrap: true);
            if (post.OptBoolean("pending"))
                NextUi.Badge(name + "Pending", row, NextStrings.Get("feed_pending"),
                    NextTheme.SkyPale, NextTheme.SkyDeep);
            return row;
        }

        /// <summary>
        /// 一張帖子卡（公共区的流與個人主頁共用）：頭像／名字／時間、摘要＋「続き」、圖、♡ 與返信數。
        /// 卡片本身去詳情；頭像另有自己的點擊（去那個人的主頁，`Heading`）。
        /// </summary>
        public static RectTransform PostCard(string prefix, RectTransform list, NextShell shell, JObject post)
        {
            var id = post.OptString("id");
            var card = NextUi.Card(prefix + "_" + id, list, 12f, 8f);
            Heading(prefix + "Head_" + id, card, shell, post);
            var body = post.OptString("body");
            if (!string.IsNullOrEmpty(body))
                UiBuilder.Text(prefix + "Body_" + id, card, Folded(body, post.OptBoolean("truncated")),
                    NextTheme.BodySize,
                    NextTheme.Ink, TextAlignmentOptions.TopLeft);
            MediaGrid(prefix + "Media_" + id, card, shell, post.OptJArray("media"));
            // 設計稿：卡片底只放輕量計數（♡ n／返信 n）；刪除只在帖文詳情頁，不放在流裡（誤觸就刪）。
            var actions = UiBuilder.Row(prefix + "Actions_" + id, card, 4f, null, TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(actions, 30f);
            LikeButton(prefix + "Like_" + id, actions, shell, post, compact: true);
            var replies = NextUi.Button(prefix + "Replies_" + id, actions,
                NextStrings.F("feed_replies_short", "count", post.OptInt("replies")),
                NextUi.Style.Ghost, 28f, 0f, null, true, NextTheme.NoteSize);
            replies.onClick.AddListener(() => shell.ShowPost(id));
            var spacer = UiBuilder.NewRect(prefix + "Spacer_" + id, actions);
            UiBuilder.Weight(spacer, 1f);
            UiBuilder.Icon(prefix + "Open_" + id, "chevron-right", actions, 16f, NextTheme.InkFaint);

            // 卡片本身只去詳情；詳情不提供任何私聊入口。
            var hit = card.gameObject.AddComponent<Image>();
            hit.color = new Color(1f, 1f, 1f, 0f);
            hit.raycastTarget = true;
            card.gameObject.AddComponent<Button>().targetGraphic = hit;
            var open = card.GetComponent<Button>();
            open.transition = Selectable.Transition.None;
            open.onClick.AddListener(() => shell.ShowPost(id));
            return card;
        }

        /// <summary>流裡只放摘要（伺服器的 excerpt 同樣是 120 字）；長的收起來，接一個「続き」，全文在詳情頁。</summary>
        public const int FoldAt = 120;

        /// <remarks>流的回包本來就只帶 120 字摘要＋`truncated`（`app/server/social/service.py`）⇒ 以旗標為準；
        /// 本地副本若已被詳情頁補成全文，就按長度自己截。</remarks>
        public static string Folded(string body, bool truncated)
        {
            // 正文是人寫的：包在 <noparse> 裡，`<` 不會被 TMP 當成標籤；只有「続き」那段自己的顏色標籤生效。
            var text = Plain(body).Replace("</noparse>", "</ noparse>");
            if (text.Length <= FoldAt && !truncated) return "<noparse>" + text + "</noparse>";
            var cut = (text.Length > FoldAt ? text.Substring(0, FoldAt) : text).TrimEnd();
            var more = ColorUtility.ToHtmlStringRGB(NextTheme.Sky);
            return "<noparse>" + cut + "…</noparse> <color=#" + more + ">" +
                   NextStrings.Get("feed_more_text") + "</color>";
        }

        /// <summary>
        /// 任何一方的頭像：角色＝她的臉、主人＝那隻貓、客人＝名字首字（聊天氣泡、會話列表、主頁共用）。
        /// </summary>
        public static RectTransform Face(string name, RectTransform parent, NextShell shell, string id, float size,
                                         Color ring)
        {
            if (shell == null || shell.IsAgent(id))
                return NextUi.Avatar(name, parent, id, size, ring);
            if (shell.PersonKind(id) == "owner" || id == "owner" ||
                (shell.IsMaster && (id == shell.ChatAccountId || id == shell.Principal)))
                return NextUi.Avatar(name, parent, OwnerAvatar, size, ring);
            return PersonAvatar(name, parent, shell.DisplayNameOf(id), size);
        }

        /// <summary>
        /// 點頭像進那個人的個人主頁（owner 2026-11-08）。頭像自己吃點擊——它在帖子卡（整張是去詳情的按鈕）裡，
        /// Unity 會把點擊交給最深一層的處理者，所以點頭像不會順帶打開詳情。
        /// </summary>
        public static void OpensProfile(RectTransform avatar, NextShell shell, JObject post)
        {
            if (avatar == null || shell == null || post == null || string.IsNullOrEmpty(post.OptString("author_id")))
                return;
            var graphic = avatar.GetComponent<Image>();
            if (graphic == null) return;
            graphic.raycastTarget = true;
            var button = avatar.gameObject.AddComponent<Button>();
            button.targetGraphic = graphic;
            button.transition = Selectable.Transition.None;
            var who = new JObject().Put("author_id", post.OptString("author_id"))
                .Put("author_kind", post.OptString("author_kind")).Put("display", post.OptString("display"));
            button.onClick.AddListener(() => shell.ShowProfile(who));
        }

        /// <summary>
        /// 人（主人／客人）的頭像：淺藍圓底＋名字第一個字。不能用 `_fallback`——那張是角色的臉，
        /// 主人的投稿會看起來像某個角色發的（M3 截圖實測）。
        /// </summary>
        public static RectTransform PersonAvatar(string name, RectTransform parent, string display, float size)
        {
            var rect = UiBuilder.NewRect(name, parent);
            UiBuilder.FixedWidth(rect, size);
            UiBuilder.FixedHeight(rect, size);
            var disc = rect.gameObject.AddComponent<Image>();
            UiBuilder.Round(disc, size / 2f);
            disc.color = NextTheme.SkyPale;
            disc.raycastTarget = false;
            var text = string.IsNullOrEmpty(display) ? "?" : display.Trim();
            var initial = text.Length > 0 ? text.Substring(0, char.IsSurrogate(text[0]) && text.Length > 1 ? 2 : 1) : "?";
            var label = UiBuilder.Text(name + "Initial", rect, initial, size * 0.42f, NextTheme.SkyDeep,
                TextAlignmentOptions.Center, noWrap: true, bold: true);
            var labelRect = (RectTransform)label.transform;
            var layout = labelRect.GetComponent<LayoutElement>() ?? labelRect.gameObject.AddComponent<LayoutElement>();
            layout.ignoreLayout = true;
            UiBuilder.Stretch(labelRect);
            return rect;
        }

        /// <summary>一或兩欄的圖片格。傳 onTap 時由呼叫端處理（投稿頁用來移除），否則看全圖。</summary>
        public static RectTransform MediaGrid(string name, RectTransform parent, NextShell shell,
                                              JArray media, bool compact = false,
                                              Action<string> onTap = null, bool removable = false)
        {
            var go = new GameObject(name, typeof(RectTransform), typeof(GridLayoutGroup), typeof(LayoutElement));
            var grid = (RectTransform)go.transform;
            grid.SetParent(parent, false);
            var count = media != null ? Mathf.Min(4, media.Length()) : 0;
            var columns = count == 1 ? 1 : 2;
            var cellWidth = compact ? 148f : (count == 1 ? 240f : 174f);
            var cellHeight = compact ? 110f : 132f;
            const float spacing = 8f;
            var layout = grid.GetComponent<GridLayoutGroup>();
            layout.cellSize = new Vector2(cellWidth, cellHeight);
            layout.spacing = new Vector2(spacing, spacing);
            layout.constraint = GridLayoutGroup.Constraint.FixedColumnCount;
            layout.constraintCount = columns;
            layout.childAlignment = TextAnchor.UpperLeft;
            var rows = count == 0 ? 0 : (count + columns - 1) / columns;
            var element = grid.GetComponent<LayoutElement>();
            element.minHeight = element.preferredHeight = rows == 0 ? 0f : rows * cellHeight + (rows - 1) * spacing;
            element.flexibleHeight = 0f;
            grid.gameObject.SetActive(count > 0);

            for (var i = 0; i < count; i++)
            {
                var id = media.OptString(i);
                if (string.IsNullOrEmpty(id)) continue;
                var cell = new GameObject(name + "_" + i, typeof(RectTransform), typeof(Image), typeof(Button));
                var rect = (RectTransform)cell.transform;
                rect.SetParent(grid, false);
                var frame = cell.GetComponent<Image>();
                frame.color = NextTheme.SkyPale;
                frame.preserveAspect = true;
                var target = id;
                var button = cell.GetComponent<Button>();
                button.targetGraphic = frame;
                button.transition = Selectable.Transition.None;
                button.onClick.AddListener(() =>
                {
                    if (onTap != null) onTap(target);
                    else shell.ShowMediaFull(target);
                });
                cell.AddComponent<NextPress>();
                shell.Api.GetMediaTexture(id, texture =>
                {
                    if (NextUi.SetOwnedSprite(frame, texture)) frame.color = Color.white;
                }, NextApi.ThumbWidth);
                if (!removable) continue;
                var close = UiBuilder.Icon(name + "Remove_" + i, "close", rect, 24f, NextTheme.Danger);
                var closeRect = (RectTransform)close.transform;
                var closeLayout = closeRect.GetComponent<LayoutElement>() ?? closeRect.gameObject.AddComponent<LayoutElement>();
                closeLayout.ignoreLayout = true;
                closeRect.anchorMin = closeRect.anchorMax = new Vector2(1f, 1f);
                closeRect.anchoredPosition = new Vector2(-5f, -5f);
            }
            return grid;
        }

        public static Button LikeButton(string name, RectTransform parent, NextShell shell, JObject post,
                                        bool compact = false)
        {
            var liked = post.OptBoolean("liked");
            var label = LikeLabel(post, liked, compact);
            var button = NextUi.Button(name, parent, label,
                liked && !compact ? NextUi.Style.Primary : NextUi.Style.Ghost,
                compact ? 28f : 32f, 0f, null, true, NextTheme.NoteSize);
            if (compact && liked) NextUi.SetColor(NextUi.LabelOf(button), NextTheme.Pink);
            button.interactable = !liked;
            button.onClick.AddListener(() =>
            {
                button.interactable = false;
                var id = post.OptString("id");
                if (shell.Api.Social != null)
                {
                    try
                    {
                        var author = new SocialRepository.Author(shell.Principal,
                            shell.IsMaster ? "owner" : "guest", shell.Principal);
                        shell.Api.Social.EnqueueLike(id, author,
                            DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0, Guid.NewGuid().ToString("N"));
                        var local = shell.Api.Social.Item(id);
                        if (local != null)
                        {
                            post.Put("likes", local.Json.OptInt("likes")).Put("liked", true);
                            NextUi.SetText(NextUi.LabelOf(button), LikeLabel(post, true, compact));
                            if (compact) NextUi.SetColor(NextUi.LabelOf(button), NextTheme.Pink);
                        }
                        shell.Api.SocialSync.Kick();
                    }
                    catch (Exception error)
                    {
                        button.interactable = true;
                        shell.Status(NextStrings.F("feed_like_fail", "reason", error.Message));
                    }
                    return;
                }
                shell.Api.Post(NextBoardPaths.Like(id),
                    new JObject(), reply =>
                    {
                        if (button == null) return;
                        if (!reply.Ok)
                        {
                            button.interactable = true;
                            shell.Status(NextStrings.F("feed_like_fail", "reason", reply.UserMessage()));
                            return;
                        }
                        var updated = reply.Json.OptJObject("post");
                        if (updated != null)
                        {
                            post.Put("likes", updated.OptInt("likes"));
                            post.Put("liked", updated.OptBoolean("liked", true));
                        }
                        NextUi.SetText(NextUi.LabelOf(button), LikeLabel(post, post.OptBoolean("liked", true), compact));
                        if (compact) NextUi.SetColor(NextUi.LabelOf(button), NextTheme.Pink);
                        button.interactable = !post.OptBoolean("liked", true);
                    });
            });
            return button;
        }

        private static string LikeLabel(JObject post, bool liked, bool compact = false)
        {
            var key = compact ? (liked ? "feed_liked_short" : "feed_like_short")
                              : (liked ? "feed_liked_count" : "feed_like_count");
            return NextStrings.F(key, "count", post != null ? post.OptInt("likes") : 0);
        }

        public static Button DeleteButton(string name, RectTransform parent, NextShell shell,
                                          JObject post, Action deleted)
        {
            var button = NextUi.Button(name, parent, NextStrings.Get("feed_delete"),
                NextUi.Style.Danger, 32f, 0f, null, true, NextTheme.NoteSize);
            // 刪除不能一按就生效（以前列表每張卡都有、誤觸就刪）：第一下變成「再按一次」，3 秒內第二下才刪。
            var armedUntil = 0f;
            button.onClick.AddListener(() =>
            {
                if (UnityEngine.Time.unscaledTime > armedUntil)
                {
                    armedUntil = UnityEngine.Time.unscaledTime + 3f;
                    NextUi.SetText(NextUi.LabelOf(button), NextStrings.Get("feed_delete_confirm"));
                    NextMotion.Play(button, 3f, _ => { }, 0f, null, () =>
                    {
                        if (button != null && UnityEngine.Time.unscaledTime >= armedUntil)
                            NextUi.SetText(NextUi.LabelOf(button), NextStrings.Get("feed_delete"));
                    });
                    return;
                }
                armedUntil = 0f;
                button.interactable = false;
                var id = post.OptString("id");
                if (shell.Api.Social != null)
                {
                    try
                    {
                        shell.Api.Social.EnqueueDelete(id, post.OptString("kind") == "reply" ||
                            !string.IsNullOrEmpty(post.OptString("parent_id")),
                            DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0, Guid.NewGuid().ToString("N"));
                        shell.Api.SocialSync.Kick();
                        deleted?.Invoke();
                    }
                    catch (Exception error)
                    {
                        button.interactable = true;
                        shell.Status(NextStrings.F("feed_delete_fail", "reason", error.Message));
                    }
                    return;
                }
                shell.Api.Delete(NextBoardPaths.Post(id), reply =>
                {
                    if (button == null) return;
                    if (!reply.Ok)
                    {
                        button.interactable = true;
                        shell.Status(NextStrings.F("feed_delete_fail", "reason", reply.UserMessage()));
                        return;
                    }
                    deleted?.Invoke();
                });
            });
            return button;
        }
    }
}
