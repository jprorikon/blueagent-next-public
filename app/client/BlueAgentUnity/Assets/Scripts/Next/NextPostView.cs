using System;
using BlueAgent.Logic;
using BlueAgent.Screens;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>帖子全文、點讚與扁平一層回覆；這裡沒有也不應長出私聊入口。</summary>
    public sealed class NextPostView
    {
        private NextShell _shell;
        private string _id = "";
        private RectTransform _list;
        private ScrollRect _scroll;
        private TMP_InputField _input;
        private Button _send;
        private RectTransform _composer;
        private int _epoch;
        private bool _sending;
        private RectTransform _headSlot;
        /// <summary>剛送出回覆：下一次重畫捲到底（新回覆在最下面），而不是回到頂。</summary>
        private bool _scrollToEnd;

        public void Build(RectTransform body, NextShell shell, string id)
        {
            _shell = shell;
            _id = id ?? "";
            _epoch++;
            _sending = false;
            var page = UiBuilder.Column("PostPage", body, 8f,
                new RectOffset(12, 12, 8, 8), true, TextAnchor.UpperLeft);
            UiBuilder.Weight(page, 1f);
            _scrollToEnd = false;
            _headSlot = NextUi.Header("PostHead", page, NextStrings.Get("feed_post_title"),
                shell.PostBack ?? shell.ShowFeed);
            _list = NextUi.ScrollArea("PostRows", page, 10f,
                new RectOffset(0, 0, 2, 8), out _scroll);
            BuildComposer(page);
            RenderLoading();
            Reload();
        }

        private void BuildComposer(RectTransform page)
        {
            _composer = UiBuilder.Row("PostComposer", page, 6f,
                new RectOffset(8, 8, 6, 6), TextAnchor.LowerCenter);
            UiBuilder.FixedHeight(_composer, 54f);
            UiBuilder.Backdrop("PostComposerBg", _composer, NextTheme.Composer, radius: 14f);
            var slot = UiBuilder.Row("PostInputSlot", _composer, 0f, null, TextAnchor.MiddleLeft);
            var slotLayout = UiBuilder.Weight(slot, 1f);
            slotLayout.minWidth = 0f;
            slotLayout.preferredWidth = 0f;
            _input = NextUi.Field("PostReplyInput", slot, NextStrings.Get("feed_reply_hint"), 42f, 18f);
            UiBuilder.Weight((RectTransform)_input.transform, 1f);
            _input.lineType = TMP_InputField.LineType.MultiLineNewline;
            _input.textComponent.textWrappingMode = TextWrappingModes.Normal;
            _input.onFocusSelectAll = false;
            _input.caretColor = NextTheme.Sky;
            _input.characterLimit = 2000;
            _send = NextUi.Button("PostReplySend", _composer, NextStrings.Get("feed_reply_send"),
                NextUi.Style.Accent, 42f, 72f);
            _send.onClick.AddListener(Reply);
        }

        private void Reload()
        {
            var epoch = _epoch;
            if (!RenderLocal()) RenderLoading();
            _shell.Api.SocialSync.Kick();
            _shell.Api.Get(NextBoardPaths.Post(_id), reply =>
            {
                if (epoch != _epoch || _list == null) return;
                if (!reply.Ok)
                {
                    if (RenderLocal()) return;
                    ClearRows();
                    NextUi.Note("PostError", _list,
                        NextStrings.F("feed_load_fail", "reason", reply.UserMessage()), NextUi.Danger);
                    if (_composer != null) _composer.gameObject.SetActive(false);
                    Settle();
                    return;
                }
                if (_composer != null) _composer.gameObject.SetActive(true);
                if (_shell.Api.Social != null)
                {
                    var post = reply.Json.OptJObject("post");
                    if (post != null) _shell.Api.Social.MergeFeed(new JArray().Put(post));
                    _shell.Api.Social.MergeReplies(_id, reply.Json.OptJArray("replies"));
                    RenderLocal();
                    return;
                }
                ClearRows();
                var serverPost = reply.Json.OptJObject("post");
                if (serverPost != null) BuildPost(serverPost);
                NextUi.Section("PostRepliesHead", _list, NextStrings.Get("feed_reply_title"), "chat");
                var replies = reply.Json.OptJArray("replies");
                if (replies == null || replies.Length() == 0)
                    NextUi.Note("PostRepliesEmpty", _list, NextStrings.Get("feed_reply_empty"));
                for (var i = 0; replies != null && i < replies.Length(); i++)
                {
                    var item = replies.OptJObject(i);
                    if (item != null) BuildReply(item);
                }
                Settle();
            });
        }

        /// <summary>
        /// Outbox 一筆有了結局：這一頁的回覆送達／被拒、或這篇的點讚落定 ⇒ 向服務端重拿一次，
        /// 回覆的「送信待ち」與「返信 N 件」才會跟上（以前要離頁再進來）。
        /// </summary>
        public void OnSocialSettled(SocialMutation mutation, NextSocialSync.Outcome outcome)
        {
            if (_list == null || outcome == NextSocialSync.Outcome.Deferred) return;
            var here = mutation.Payload != null && mutation.Payload.OptString("id") == _id;
            if (mutation.Kind.StartsWith("reply.", StringComparison.Ordinal) ||
                (here && mutation.Kind == "post.like"))
                Reload();
        }

        private bool RenderLocal()
        {
            if (_shell.Api.Social == null || _list == null) return false;
            var post = _shell.Api.Social.Item(_id);
            if (post == null) return false;
            ClearRows();
            if (_composer != null) _composer.gameObject.SetActive(true);
            BuildPost(post.Json);
            NextUi.Section("PostRepliesHead", _list, NextStrings.Get("feed_reply_title"), "chat");
            var replies = _shell.Api.Social.Replies(_id);
            if (replies.Count == 0) NextUi.Note("PostRepliesEmpty", _list, NextStrings.Get("feed_reply_empty"));
            foreach (var reply in replies) BuildReply(reply.Json);
            Settle();
            return true;
        }

        private void BuildPost(JObject post)
        {
            var card = NextUi.Card("PostDetail", _list, 12f, 9f);
            NextBoardUi.Heading("PostDetailHead", card, _shell, post);
            var body = post.OptString("body");
            if (!string.IsNullOrEmpty(body))
                UiBuilder.Text("PostDetailBody", card, "<noparse>" + NextBoardUi.Plain(body)
                    .Replace("</noparse>", "</ noparse>") + "</noparse>", NextTheme.BodySize,
                    NextTheme.Ink, TextAlignmentOptions.TopLeft);
            NextBoardUi.MediaGrid("PostDetailMedia", card, _shell, post.OptJArray("media"));
            var actions = UiBuilder.Row("PostDetailActions", card, 6f, null, TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(actions, 34f);
            NextBoardUi.LikeButton("PostDetailLike", actions, _shell, post);
            NextUi.Badge("PostDetailReplyCount", actions,
                NextStrings.F("feed_replies", "count", post.OptInt("replies")),
                NextTheme.SkyPale, NextTheme.SkyDeep);
            // 設計稿：刪除在頁首右上（不跟點讚擠在一排）；重畫時換掉舊的那顆
            if (_headSlot != null)
            {
                var old = _headSlot.Find("PostDetailDelete");
                if (old != null) UnityEngine.Object.Destroy(old.gameObject);
                if (NextBoardUi.CanDelete(_shell, post))
                    NextBoardUi.DeleteButton("PostDetailDelete", _headSlot, _shell, post,
                        () => { _shell.ShowFeed(); _shell.Status(NextStrings.Get("feed_deleted")); });
            }
        }

        private void BuildReply(JObject reply)
        {
            var id = reply.OptString("id");
            var card = NextUi.Card("PostReply_" + id, _list, 10f, 6f);
            NextBoardUi.Heading("PostReplyHead_" + id, card, _shell, reply);
            var body = reply.OptString("body");
            if (!string.IsNullOrEmpty(body))
                UiBuilder.Text("PostReplyBody_" + id, card, body, NextTheme.BodySize,
                    NextTheme.Ink, TextAlignmentOptions.TopLeft);
            NextBoardUi.MediaGrid("PostReplyMedia_" + id, card, _shell, reply.OptJArray("media"), true);
            if (NextBoardUi.CanDelete(_shell, reply))
            {
                var actions = UiBuilder.Row("PostReplyActions_" + id, card, 6f, null, TextAnchor.MiddleRight);
                UiBuilder.FixedHeight(actions, 32f);
                NextBoardUi.DeleteButton("PostReplyDelete_" + id, actions, _shell, reply, Reload);
            }
        }

        private void Reply()
        {
            if (_sending || _input == null) return;
            var text = (_input.text ?? "").Trim();
            if (string.IsNullOrEmpty(text)) return;
            _sending = true;
            _send.interactable = false;
            var epoch = _epoch;
            if (_shell.Api.Social != null)
            {
                try
                {
                    var author = new SocialRepository.Author(_shell.Principal,
                        _shell.IsMaster ? "owner" : "guest",
                        _shell.Me.OptString("display", _shell.Me.OptString("name", _shell.Principal)));
                    _shell.Api.Social.EnqueueReply(_id, text, new JArray(), author,
                        DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0, Guid.NewGuid().ToString("N"));
                    _input.text = "";
                    _sending = false;
                    _send.interactable = true;
                    _scrollToEnd = true;
                    RenderLocal();
                    _shell.Api.SocialSync.Kick();
                }
                catch (Exception error)
                {
                    _sending = false;
                    _send.interactable = true;
                    _shell.Status(NextStrings.F("feed_reply_fail", "reason", error.Message));
                }
                return;
            }
            _shell.Api.Post(NextBoardPaths.Replies(_id),
                new JObject().Put("text", text).Put("media", new JArray()), reply =>
                {
                    if (epoch != _epoch || _input == null) return;
                    _sending = false;
                    _send.interactable = true;
                    if (!reply.Ok)
                    {
                        _shell.Status(NextStrings.F("feed_reply_fail", "reason", reply.UserMessage()));
                        return;
                    }
                    _input.text = "";
                    _scrollToEnd = true;
                    Reload();
                });
        }

        private void RenderLoading()
        {
            if (_list == null) return;
            ClearRows();
            NextUi.Note("PostLoading", _list, NextStrings.Get("loading"));
            Settle();
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
            if (_scroll != null) _scroll.verticalNormalizedPosition = _scrollToEnd ? 0f : 1f;
        }
    }
}
