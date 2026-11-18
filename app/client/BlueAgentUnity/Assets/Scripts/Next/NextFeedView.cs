using System;
using BlueAgent.Logic;
using BlueAgent.Screens;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>M3 首頁：今天的公共動態與聊天入口。不分角色、不掛舞台；這一級沒有聊天輸入列。</summary>
    public sealed class NextFeedView
    {
        private const int PageSize = 20;
        private NextShell _shell;
        private RectTransform _list;
        private ScrollRect _scroll;
        private Component _today;
        private LayoutElement _todayLayout;
        private CanvasGroup _todayGroup;
        private bool _todayShown = true;
        private float _lastScrollY;
        /// <summary>「今日の投稿 N 件」那一行的高度（收起＝0）。</summary>
        private const float TodayHeight = 22f;
        private Button _more;
        private int _epoch;
        private int _shown;
        private string _nextCursor = "";
        private bool _loadingMore;

        public void Build(RectTransform body, NextShell shell)
        {
            _shell = shell;
            _epoch++;
            _nextCursor = "";
            _shown = 0;
            _loadingMore = false;

            // 公共区不区分角色（owner 2026-11-07）：没有舞台、没有角色切换，单栏只放流；
            // 大屏由 Shell 的 CapBody 置中限宽，不再左舞台右流。
            var page = UiBuilder.Column("FeedPage", body, 8f,
                new RectOffset(12, 12, 8, 8), true, TextAnchor.UpperLeft);
            UiBuilder.Weight(page, 1f);
            var feed = page;

            // 公共区不再有「標題＋重新整理」那一排（owner 2026-11-08）：聊天入口與未讀在頂欄
            // （`NextShell` 的 TopChat），更新改成下拉（`NextPullRefresh`）。
            RefreshUnread();
            _today = UiBuilder.Text("FeedToday", feed, "", NextTheme.NoteSize, NextTheme.InkSoft,
                TextAlignmentOptions.MidlineLeft, noWrap: true, bold: true);
            _todayLayout = UiBuilder.FixedHeight((RectTransform)_today.transform, TodayHeight);
            _todayGroup = _today.gameObject.AddComponent<CanvasGroup>();
            _todayShown = true;
            _lastScrollY = 0f;

            _list = NextUi.ScrollArea("FeedRows", feed, 10f,
                new RectOffset(0, 0, 2, 10), out _scroll);
            BuildPullRefresh();
            _scroll.onValueChanged.AddListener(_ => OnScrolled());
            BuildFab(page);
            RenderLoading();
            Reload();
        }

        /// <summary>下拉更新：流可以往下多拉一點（Elastic），拉過門檻放開就重新載入。</summary>
        private void BuildPullRefresh()
        {
            _scroll.movementType = ScrollRect.MovementType.Elastic;
            _scroll.elasticity = 0.12f;
            var hint = UiBuilder.Text("FeedPullHint", _scroll.viewport, NextStrings.Get("feed_pull_hint"),
                NextTheme.NoteSize, NextTheme.InkSoft, TextAlignmentOptions.Center, noWrap: true);
            var rect = (RectTransform)hint.transform;
            var layout = rect.GetComponent<LayoutElement>() ?? rect.gameObject.AddComponent<LayoutElement>();
            layout.ignoreLayout = true;
            rect.anchorMin = new Vector2(0f, 1f);
            rect.anchorMax = new Vector2(1f, 1f);
            rect.pivot = new Vector2(0.5f, 1f);
            rect.sizeDelta = new Vector2(0f, 28f);
            rect.anchoredPosition = new Vector2(0f, -8f);
            rect.SetAsFirstSibling();                       // 在卡片底下：拉開了才看得到
            var pull = _scroll.gameObject.AddComponent<NextPullRefresh>();
            pull.Scroll = _scroll;
            pull.Hint = hint as TMP_Text;
            pull.PullText = NextStrings.Get("feed_pull_hint");
            pull.ReleaseText = NextStrings.Get("feed_release_hint");
            pull.OnRefresh = Reload;
            if (pull.Hint != null) pull.Hint.alpha = 0f;
        }

        /// <summary>
        /// 往上滑（往下看）時收起「今日の投稿 N 件」，往回滑或回到頂端再出來（owner 2026-11-08）。
        /// 以內容的捲動量判方向；小於 6 的抖動不算。
        /// </summary>
        private void OnScrolled()
        {
            if (_scroll == null || _scroll.content == null) return;
            var y = _scroll.content.anchoredPosition.y;
            var delta = y - _lastScrollY;
            _lastScrollY = y;
            if (y <= 8f) ShowToday(true);
            else if (delta > 6f) ShowToday(false);
            else if (delta < -6f) ShowToday(true);
        }

        private void ShowToday(bool show)
        {
            if (_todayShown == show || _todayLayout == null) return;
            _todayShown = show;
            var layout = _todayLayout;
            var group = _todayGroup;
            NextMotion.Play(_today, 0.18f, p =>
            {
                var t = show ? p : 1f - p;
                layout.minHeight = layout.preferredHeight = TodayHeight * t;
                if (group != null) group.alpha = t;
            });
        }

        private void BuildFab(RectTransform page)
        {
            var layer = UiBuilder.Overlay("FeedFabLayer", page);
            // 設計稿：右下「＋投稿」膠囊（有字），不是只有一個圓形 ＋
            var fab = NextUi.Button("FeedCompose", layer, NextStrings.Get("feed_publish"),
                NextUi.Style.Accent, 46f, 116f, "plus");
            var rect = (RectTransform)fab.transform;
            var element = rect.GetComponent<LayoutElement>() ?? rect.gameObject.AddComponent<LayoutElement>();
            element.ignoreLayout = true;
            rect.anchorMin = rect.anchorMax = rect.pivot = new Vector2(1f, 0f);
            rect.sizeDelta = new Vector2(116f, 46f);
            rect.anchoredPosition = new Vector2(-12f, 12f);
            rect.SetAsLastSibling();
            fab.onClick.AddListener(_shell.ShowCompose);
        }

        public void RefreshUnread()
        {
            _shell?.RefreshTopUnread();
        }

        /// <summary>上一次拉公共區失敗了沒（連線恢復時只重拉失敗過的，不打斷正在看的人）。</summary>
        private bool _fetchFailed;

        /// <summary>連線恢復時叫（`NextShell.OnEventsState`）：上次沒拉到才重拉。</summary>
        public void RetryIfFailed()
        {
            if (_fetchFailed && _list != null) Reload();
        }

        private void Reload()
        {
            if (_shell == null) return;
            _epoch++;
            var epoch = _epoch;
            _nextCursor = "";
            _shown = 0;
            _loadingMore = false;
            if (!RenderLocal()) RenderLoading();
            _shell.Api.SocialSync.Kick();
            var since = new DateTimeOffset(DateTime.Today).ToUnixTimeSeconds();
            _shell.Api.Get(NextBoardPaths.Feed(PageSize, since), reply =>
            {
                if (epoch != _epoch || _list == null) return;
                _fetchFailed = !reply.Ok;
                if (!reply.Ok)
                {
                    // 有本地副本也要讓人看到「沒拉到」：否則失敗和「今天沒有動態」長得一模一樣。
                    if (RenderLocal()) ShowFetchError(reply);
                    else RenderError(reply);
                    return;
                }
                if (_shell.Api.Social != null)
                {
                    MergeAndRender(reply.Json);
                    return;
                }
                ClearRows();
                _more = null;
                Append(reply.Json);
                Settle(true);
            });
        }

        private void LoadMore()
        {
            if (_loadingMore || string.IsNullOrEmpty(_nextCursor)) return;
            _loadingMore = true;
            var epoch = _epoch;
            if (_more != null) _more.gameObject.SetActive(false);
            var cursor = _nextCursor;
            var since = new DateTimeOffset(DateTime.Today).ToUnixTimeSeconds();
            _shell.Api.Get(NextBoardPaths.Feed(PageSize, since, cursor), reply =>
            {
                if (epoch != _epoch || _list == null) return;
                _loadingMore = false;
                if (!reply.Ok)
                {
                    if (_more != null) _more.gameObject.SetActive(true);
                    _shell.Status(NextStrings.F("feed_load_fail", "reason", reply.UserMessage()));
                    return;
                }
                if (_shell.Api.Social != null) MergeAndRender(reply.Json);
                else
                {
                    Append(reply.Json);
                    Settle(false);
                }
            });
        }

        /// <summary>
        /// Outbox 一筆有了結局：只在投稿／刪帖時重畫（新帖在最上面，回頂不會打斷閱讀）；
        /// 點讚本地已即時反映，不必為它把整頁重排。
        /// </summary>
        public void OnSocialSettled(SocialMutation mutation, NextSocialSync.Outcome outcome)
        {
            if (_list == null || outcome == NextSocialSync.Outcome.Deferred) return;
            if (mutation.Kind == "post.create" || mutation.Kind == "post.delete") RenderLocal();
        }

        private void MergeAndRender(JObject json)
        {
            _shell.Api.Social.MergeFeed(json != null ? json.OptJArray("posts") : null);
            _nextCursor = json != null ? json.OptString("next_cursor") : "";
            RenderLocal();
        }

        private bool RenderLocal()
        {
            if (_shell.Api.Social == null || _list == null) return false;
            var posts = _shell.Api.Social.Feed(500);
            ClearRows();
            _more = null;
            _shown = 0;
            foreach (var item in posts)
            {
                if (!IsToday(item.Json.OptDouble("at"))) continue;
                BuildPost(item.Json, _shown);
                _shown++;
            }
            NextUi.SetText(_today, NextStrings.F("feed_today_count", "count", _shown));
            if (_shown == 0) BuildEmpty();
            if (!string.IsNullOrEmpty(_nextCursor))
            {
                _more = NextUi.Button("FeedMore", _list, NextStrings.Get("feed_more"),
                    NextUi.Style.Secondary, 36f, 0f, "chevron-down");
                _more.onClick.AddListener(LoadMore);
            }
            Settle(true);
            return true;
        }

        private void Append(JObject json)
        {
            var posts = json != null ? json.OptJArray("posts") : null;
            for (var i = 0; posts != null && i < posts.Length(); i++)
            {
                var post = posts.OptJObject(i);
                if (post == null || !IsToday(post.OptDouble("at"))) continue;
                BuildPost(post, i);
                _shown++;
            }
            _nextCursor = json != null ? json.OptString("next_cursor") : "";
            NextUi.SetText(_today, NextStrings.F("feed_today_count", "count", _shown));
            if (_shown == 0) BuildEmpty();
            if (!string.IsNullOrEmpty(_nextCursor))
            {
                _more = NextUi.Button("FeedMore", _list, NextStrings.Get("feed_more"),
                    NextUi.Style.Secondary, 36f, 0f, "chevron-down");
                _more.onClick.AddListener(LoadMore);
            }
        }

        private static bool IsToday(double at)
        {
            if (at <= 0) return false;
            try { return DateTimeOffset.FromUnixTimeSeconds((long)at).LocalDateTime.Date == DateTime.Today; }
            catch (ArgumentOutOfRangeException) { return false; }
        }

        private void BuildPost(JObject post, int index)
        {
            NextBoardUi.PostCard("FeedPost", _list, _shell, post);
        }

        /// <summary>今天還沒有帖子：一張說明卡（帖子會出現在這裡、右下可以投稿），不是一行灰字掛在空白頁上。</summary>
        private void BuildEmpty()
        {
            var card = NextUi.Card("FeedEmpty", _list, 14f, 6f);
            UiBuilder.Text("FeedEmptyTitle", card, NextStrings.Get("feed_empty_today"), NextTheme.BodySize,
                NextTheme.Ink, TextAlignmentOptions.TopLeft, bold: true);
            NextUi.Note("FeedEmptyHint", card, NextStrings.Get("feed_empty_hint"));
        }

        private void RenderLoading()
        {
            if (_list == null) return;
            ClearRows();
            _more = null;
            NextUi.SetText(_today, NextStrings.Get("feed_today_loading"));
            NextUi.Note("FeedLoading", _list, NextStrings.Get("loading"));
            Settle(true);
        }

        private void RenderError(NextApi.Reply reply)
        {
            ClearRows();
            _more = null;
            NextUi.SetText(_today, NextStrings.F("feed_today_count", "count", 0));
            NextUi.Note("FeedError", _list,
                NextStrings.F("feed_load_fail", "reason", reply.UserMessage()), NextUi.Danger);
            var retry = NextUi.Button("FeedRetry", _list, NextStrings.Get("feed_retry"),
                NextUi.Style.Secondary, 36f, 0f, "refresh");
            retry.onClick.AddListener(Reload);
            Settle(true);
        }

        /// <summary>本地副本照畫，頂上加失敗原因與重試；空狀態那句「今天還沒有動態」此時不成立，拿掉。</summary>
        private void ShowFetchError(NextApi.Reply reply)
        {
            if (_list == null) return;
            var empty = _list.Find("FeedEmpty");
            if (empty != null) UnityEngine.Object.Destroy(empty.gameObject);
            var retry = NextUi.Button("FeedRetry", _list, NextStrings.Get("feed_retry"),
                NextUi.Style.Secondary, 36f, 0f, "refresh");
            retry.onClick.AddListener(Reload);
            retry.transform.SetAsFirstSibling();
            var note = NextUi.Note("FeedError", _list,
                NextStrings.F("feed_load_fail", "reason", reply.UserMessage()), NextUi.Danger);
            note.transform.SetAsFirstSibling();
            Settle(true);
        }

        private void ClearRows()
        {
            if (_list == null) return;
            for (var i = _list.childCount - 1; i >= 0; i--)
                UnityEngine.Object.Destroy(_list.GetChild(i).gameObject);
        }

        private void Settle(bool top)
        {
            if (_list == null) return;
            UiBuilder.Settle(_list);
            if (top && _scroll != null) _scroll.verticalNormalizedPosition = 1f;
            if (top) ShowToday(true);
            else if (!_todayShown && _todayLayout != null)    // Settle 會把行高釘回字高 ⇒ 收起時補回 0
                _todayLayout.minHeight = _todayLayout.preferredHeight = 0f;
        }
    }
}
