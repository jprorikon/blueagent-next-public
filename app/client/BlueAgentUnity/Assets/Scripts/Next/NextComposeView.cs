using System;
using System.Collections.Generic;
using BlueAgent.Logic;
using BlueAgent.Screens;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>獨立投稿頁。選圖先走既有 UploadMedia，正式投稿只提交 media id。</summary>
    public sealed class NextComposeView
    {
        private NextShell _shell;
        private TMP_InputField _input;
        private RectTransform _mediaHost;
        private Component _status;
        private Button _add;
        private Button _publish;
        private readonly List<string> _media = new List<string>();
        private int _uploading;
        private int _epoch;

        public void Build(RectTransform body, NextShell shell)
        {
            _shell = shell;
            _epoch++;
            // 上一次沒發出去的離線佔位圖：草稿不留，端末那份也別留著（發出去的在 Publish 已清單、檔案歸 Outbox 管）
            foreach (var id in _media) NextOutboxMedia.Delete(id);
            _media.Clear();
            _uploading = 0;
            var page = UiBuilder.Column("ComposePage", body, 10f,
                new RectOffset(12, 12, 8, 12), true, TextAnchor.UpperLeft);
            UiBuilder.Weight(page, 1f);
            var slot = NextUi.Header("ComposeHead", page, NextStrings.Get("feed_compose"), shell.ShowFeed);
            _publish = NextUi.Button("ComposePublish", slot, NextStrings.Get("feed_publish"),
                NextUi.Style.Accent, 36f, 86f);
            _publish.onClick.AddListener(Publish);

            var visibility = UiBuilder.Row("ComposeVisibility", page, 8f, null, TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(visibility, 28f);
            NextUi.Badge("ComposePublic", visibility, NextStrings.Get("feed_visibility"),
                NextTheme.SkyPale, NextTheme.SkyDeep, NextTheme.Sky);
            // 誰看得到、誰能刪：設計稿的說明框；整段要折行，不能塞在徽章那一排
            NextUi.Note("ComposePublicHint", page, NextStrings.Get("feed_public_hint"));

            _input = NextUi.Field("ComposeInput", page, NextStrings.Get("feed_compose_hint"), 220f);
            _input.lineType = TMP_InputField.LineType.MultiLineNewline;
            _input.textComponent.textWrappingMode = TextWrappingModes.Normal;
            _input.textComponent.alignment = TextAlignmentOptions.TopLeft;
            // 提示字也從左上開始（以前停在 220pt 框的正中間）
            if (_input.placeholder is TMP_Text placeholder) placeholder.alignment = TextAlignmentOptions.TopLeft;
            _input.onFocusSelectAll = false;
            _input.caretColor = NextTheme.Sky;
            _input.characterLimit = 2000;

            var tools = UiBuilder.Row("ComposeTools", page, 8f, null, TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(tools, 42f);
            _add = NextUi.Button("ComposeAddPhoto", tools, NextStrings.Get("feed_add_photo"),
                NextUi.Style.Secondary, 40f, 0f, "image");
            _add.onClick.AddListener(_shell.PickComposeImage);
            NextUi.Note("ComposePhotoLimit", tools, NextStrings.Get("feed_photo_limit"));
            _mediaHost = UiBuilder.Column("ComposeMedia", page, 6f, null, true, TextAnchor.UpperLeft);
            _status = UiBuilder.Text("ComposeStatus", page, "", NextTheme.NoteSize, NextTheme.InkSoft,
                TextAlignmentOptions.MidlineLeft, noWrap: true);
            UiBuilder.FixedHeight((RectTransform)_status.transform, 24f);
            RefreshMedia();
        }

        /// <summary>離頁後舊上傳回呼不能寫進下一次投稿。</summary>
        public void Deactivate()
        {
            _epoch++;
            _input = null;
            _mediaHost = null;
            _add = null;
            _publish = null;
            _status = null;
        }

        public void AddPickedImage(byte[] bytes)
        {
            if (bytes == null || bytes.Length == 0 || _input == null) return;
            if (_media.Count + _uploading >= 4)
            {
                SetStatus(NextStrings.Get("feed_photo_limit"), true);
                return;
            }
            _uploading++;
            var epoch = _epoch;
            SetStatus(NextStrings.Get("feed_uploading"), false);
            RefreshButtons();
            _shell.Api.UploadMedia(bytes, "", json =>
            {
                if (epoch != _epoch || _input == null) return;
                _uploading--;
                var id = json != null ? json.OptString("id") : "";
                if (!string.IsNullOrEmpty(id) && !_media.Contains(id) && _media.Count < 4) _media.Add(id);
                SetStatus("", false);
                RefreshMedia();
            }, (status, reason) =>
            {
                if (epoch != _epoch || _input == null) return;
                _uploading--;
                // 連不上（不是被拒收）⇒ 原圖先存端末、帖子裡放佔位，Outbox 連上後先傳圖再發帖（NextOutboxMedia）
                var queued = NextOutboxMedia.IsTransport(status) && _shell.Api.Social != null && _media.Count < 4
                    ? NextOutboxMedia.Save(bytes) : "";
                if (!string.IsNullOrEmpty(queued))
                {
                    _media.Add(queued);
                    SetStatus(NextStrings.Get("feed_photo_queued"), false);
                }
                else SetStatus(NextStrings.F("feed_upload_fail", "reason", reason), true);
                RefreshMedia();
            });
        }

        private void RemoveMedia(string id)
        {
            if (_uploading > 0) return;
            _media.Remove(id);
            NextOutboxMedia.Delete(id);                // 佔位圖撤掉就連端末那份一起刪（伺服器 id 這支什麼都不做）
            SetStatus("", false);
            RefreshMedia();
        }

        private void RefreshMedia()
        {
            if (_mediaHost == null) return;
            for (var i = _mediaHost.childCount - 1; i >= 0; i--)
                UnityEngine.Object.Destroy(_mediaHost.GetChild(i).gameObject);
            NextBoardUi.MediaGrid("ComposeMediaGrid", _mediaHost, _shell,
                JArray.OfStrings(_media), true, RemoveMedia, true);
            RefreshButtons();
            UiBuilder.Settle(_mediaHost);
        }

        private void RefreshButtons()
        {
            if (_add != null) _add.interactable = _media.Count + _uploading < 4;
            if (_publish != null) _publish.interactable = _uploading == 0;
        }

        private void Publish()
        {
            if (_input == null || _uploading > 0) return;
            var text = (_input.text ?? "").Trim();
            if (string.IsNullOrEmpty(text) && _media.Count == 0)
            {
                SetStatus(NextStrings.Get("feed_empty_post"), true);
                return;
            }
            var epoch = _epoch;
            _publish.interactable = false;
            SetStatus(NextStrings.Get("feed_publishing"), false);
            if (_shell.Api.Social != null)
            {
                try
                {
                    var author = new SocialRepository.Author(_shell.Principal,
                        _shell.IsMaster ? "owner" : "guest",
                        _shell.Me.OptString("display", _shell.Me.OptString("name", _shell.Principal)));
                    _shell.Api.Social.EnqueuePost(text, JArray.OfStrings(_media), author,
                        DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0, Guid.NewGuid().ToString("N"));
                    _input.text = "";
                    _media.Clear();
                    _shell.ShowFeed();
                    // 結果提示交給殼的 OnSocialSettled：送達說「投稿しました」，連不上才說「オンライン後に送信します」
                    // （以前入隊就一律說後者，在線時也一樣，像是沒發出去）。
                    _shell.Api.SocialSync.Kick();
                }
                catch (Exception error)
                {
                    _publish.interactable = true;
                    SetStatus(NextStrings.F("feed_publish_fail", "reason", error.Message), true);
                }
                return;
            }
            var body = new JObject().Put("text", text).Put("media", JArray.OfStrings(_media));
            _shell.Api.Post(NextBoardPaths.Posts, body, reply =>
            {
                if (epoch != _epoch || _input == null) return;
                if (!reply.Ok)
                {
                    _publish.interactable = true;
                    SetStatus(NextStrings.F("feed_publish_fail", "reason", reply.UserMessage()), true);
                    return;
                }
                _input.text = "";
                _media.Clear();
                _shell.ShowFeed();
                _shell.Status(NextStrings.Get("feed_posted"));
            });
        }

        private void SetStatus(string text, bool error)
        {
            NextUi.SetText(_status, text);
            NextUi.SetColor(_status, error ? NextUi.Danger : NextTheme.InkSoft);
        }
    }
}
