using BlueAgent.Logic;
using BlueAgent.Screens;
using TMPro;
using UnityEngine;
using UnityEngine.Events;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>
    /// 個人主頁（owner 2026-11-08）：從公共区／帖子詳情點頭像進來。
    ///   · 角色：上面是她的舞台（立繪＋公開心情徽章）；主人／客人：一張橫幅（先固定一張圖）＋公開心情。
    ///   · 下面是這個人的帖子履歷：跨天、最新在前，「もっと見る」往前翻。
    ///   · 「1:1 で話す」：角色的主頁直接進她的私聊；主人看客人、客人看主人時開兩人之間的私聊（P-C2，Agent 看不到）。
    ///   · 只有**自己的**主頁才有「＋投稿」。
    /// </summary>
    public sealed class NextProfileView
    {
        private const int PageSize = 20;
        /// <summary>主人／客人主頁的橫幅：先固定一張（owner「先随便固定一张图」）。</summary>
        public const string BannerImage = "backgrounds/street";
        private const float BannerHeight = 132f;

        private NextShell _shell;
        private JObject _who;
        private RectTransform _list;
        private ScrollRect _scroll;
        private Button _more;
        private string _cursor = "";
        private int _shown;
        private int _epoch;

        public string AccountId { get { return _who != null ? _who.OptString("author_id") : ""; } }
        public bool Alive { get { return _list != null; } }
        private bool IsEntity { get { return _who != null && _who.OptString("author_kind") == "entity"; } }

        public bool IsSelf(NextShell shell)
        {
            var id = AccountId;
            return !string.IsNullOrEmpty(id) && (id == shell.ChatAccountId || id == shell.Principal);
        }

        public void Build(RectTransform body, NextShell shell, JObject who, UnityAction back)
        {
            _shell = shell;
            _who = who;
            _epoch++;
            _cursor = "";
            _shown = 0;
            _more = null;
            var name = NextBoardUi.DisplayName(shell, who);
            var page = UiBuilder.Column("ProfilePage", body, 8f, new RectOffset(12, 12, 8, 8), true,
                TextAnchor.UpperLeft);
            UiBuilder.Weight(page, 1f);
            NextUi.Header("ProfileHead", page, name, back);
            if (IsEntity) BuildStage(page);
            else BuildBanner(page);
            BuildIdentity(page, name);
            NextUi.Section("ProfilePostsHead", page, NextStrings.Get("profile_posts"), "chat");
            _list = NextUi.ScrollArea("ProfileRows", page, 10f, new RectOffset(0, 0, 2, 64), out _scroll);
            if (IsSelf(shell)) BuildCompose(page);
            Load(false);
        }

        /// <summary>角色：跟 1:1 聊天頁同一套舞台與心情徽章（`NextStageView.Build` 的 moodOf）。</summary>
        private void BuildStage(RectTransform page)
        {
            var id = AccountId;
            try
            {
                NextStageView.Build(page, id, AvatarTimeline.Normal, NextConfig.BackgroundFor(id), false,
                    _shell, id, NextStageView.ChatStageHeight);
                NextStageView.Frame(page.Find("Stage") as RectTransform);
            }
            catch (System.Exception error)
            {
                Debug.LogWarning("[next] 個人主頁立繪載入失敗：" + error.Message);
            }
        }

        /// <summary>主人／客人：圓角橫幅（圖鋪滿裁切）＋左上角的公開心情徽章。</summary>
        private void BuildBanner(RectTransform page)
        {
            var banner = UiBuilder.NewRect("ProfileBanner", page);
            UiBuilder.FixedHeight(banner, BannerHeight);
            banner.gameObject.AddComponent<RectMask2D>().softness = new Vector2Int(0, 0);
            var picture = UiBuilder.Overlay("ProfileBannerImage", banner);
            var image = picture.gameObject.AddComponent<Image>();
            image.raycastTarget = false;
            var texture = Resources.Load<Texture2D>(BannerImage);
            if (texture != null)
            {
                NextUi.SetOwnedSprite(image, texture, ownTexture: false);
                var fitter = picture.gameObject.AddComponent<AspectRatioFitter>();
                fitter.aspectMode = AspectRatioFitter.AspectMode.EnvelopeParent;
                fitter.aspectRatio = texture.height > 0 ? (float)texture.width / texture.height : 1f;
            }
            else
            {
                image.color = NextTheme.SkyPale;
            }
            var edge = UiBuilder.Overlay("ProfileBannerEdge", banner);
            UiBuilder.Border(edge, NextTheme.PanelLine, 1f, NextTheme.Radius);
            NextMoodBadge.Attach(banner, _shell, AccountId);
        }

        /// <summary>頭像（角色已有舞台就不重複放）＋名字＋「1:1 で話す」。</summary>
        private void BuildIdentity(RectTransform page, string name)
        {
            var row = UiBuilder.Row("ProfileIdentity", page, 10f, new RectOffset(2, 2, 0, 0), TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(row, 46f);
            if (!IsEntity)
            {
                if (NextBoardUi.IsOwner(_who))
                    NextUi.Avatar("ProfileAvatar", row, NextBoardUi.OwnerAvatar, 42f, NextTheme.Sky);
                else
                    NextBoardUi.PersonAvatar("ProfileAvatar", row, name, 42f);
            }
            var label = UiBuilder.Text("ProfileName", row, name, NextTheme.TitleSize, NextTheme.Ink,
                TextAlignmentOptions.MidlineLeft, noWrap: true, bold: true);
            var layout = UiBuilder.Weight((RectTransform)label.transform, 1f);
            layout.minWidth = 0f;
            if (IsSelf(_shell) || !(IsEntity || CanDirect)) return;
            var id = AccountId;
            var kind = _who.OptString("author_kind");
            var chat = NextUi.Button("ProfileChat", row, NextStrings.Get("profile_chat"),
                NextUi.Style.Accent, 38f, 0f, "chat");
            if (IsEntity) chat.onClick.AddListener(() => _shell.SwitchEntity(id, _shell.EntityNameOf(id)));
            else chat.onClick.AddListener(() => _shell.OpenDirect(id, kind, name));
        }

        /// <summary>人與人之間只開主人↔客人（App Server C-09：客人之間不互通）。</summary>
        private bool CanDirect
        {
            get
            {
                var kind = _who != null ? _who.OptString("author_kind") : "";
                return _shell.IsMaster ? kind == "guest" : kind == "owner";
            }
        }

        /// <summary>自己的主頁才有：右下「＋投稿」（與公共区同一顆）。</summary>
        private void BuildCompose(RectTransform page)
        {
            var layer = UiBuilder.Overlay("ProfileFabLayer", page);
            var fab = NextUi.Button("ProfileCompose", layer, NextStrings.Get("feed_publish"),
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

        /// <summary>這個人的帖子：`/v1/feed?authors=` 不帶 since ⇒ 跨天、最新在前；`next_cursor` 往前翻。</summary>
        private void Load(bool more)
        {
            var epoch = _epoch;
            if (!more)
            {
                ClearRows();
                NextUi.Note("ProfileLoading", _list, NextStrings.Get("loading"));
            }
            if (_more != null) _more.interactable = false;
            _shell.Api.Get(NextBoardPaths.AuthorFeed(AccountId, PageSize, more ? _cursor : ""), reply =>
            {
                if (epoch != _epoch || _list == null) return;
                if (!more) ClearRows();
                if (_more != null) Object.Destroy(_more.gameObject);
                _more = null;
                if (!reply.Ok)
                {
                    NextUi.Note("ProfileError", _list,
                        NextStrings.F("feed_load_fail", "reason", reply.UserMessage()), NextUi.Danger);
                    Settle(!more);
                    return;
                }
                var posts = reply.Json.OptJArray("posts");
                for (var i = 0; posts != null && i < posts.Length(); i++)
                {
                    var post = posts.OptJObject(i);
                    if (post == null) continue;
                    NextBoardUi.PostCard("ProfilePost", _list, _shell, post);
                    _shown++;
                }
                if (_shown == 0)
                    NextUi.Note("ProfileEmpty", _list, NextStrings.Get("profile_empty"));
                _cursor = reply.Json.OptString("next_cursor");
                if (!string.IsNullOrEmpty(_cursor))
                {
                    _more = NextUi.Button("ProfileMore", _list, NextStrings.Get("feed_more"),
                        NextUi.Style.Secondary, 36f, 0f, "chevron-down");
                    _more.onClick.AddListener(() => Load(true));
                }
                Settle(!more);
            });
        }

        private void ClearRows()
        {
            if (_list == null) return;
            for (var i = _list.childCount - 1; i >= 0; i--)
                Object.Destroy(_list.GetChild(i).gameObject);
        }

        private void Settle(bool top)
        {
            if (_list == null) return;
            UiBuilder.Settle(_list);
            if (top && _scroll != null) _scroll.verticalNormalizedPosition = 1f;
        }
    }
}
