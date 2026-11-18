using System;
using BlueAgent.Logic;
using BlueAgent.Screens;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>
    /// 人類帖主的通知（owner 2026-11-16 定 B.App.8：**App 內提醒條＋系統推送，先只做 Android**）。
    ///
    /// 來源只有一個：App Server 的 `notice.created`（別人回了／讚了你的帖，只有帖主讀得到）。
    /// · App 在前景：頂欄下面浮一條提醒，點了開那篇帖；不在焦點（拉下通知欄之類）再加一則系統通知。
    /// · App 在背景：Unity 主迴圈停了，由 Java 的 `NoticeListen` 接手同一條 SSE 響系統通知
    ///   （私聊也在那裡響）。這裡負責把位址／token／自己的帳號／暫停那一刻的游標交過去。
    /// </summary>
    public static class NextNotices
    {
        private static bool _asked;

        /// <summary>登入後與每次暫停前叫；`on=false`＝登出（Java 那條停掉）。</summary>
        public static void Configure(NextShell shell, bool on)
        {
            if (shell == null || shell.Api == null) return;
            NativeShell.ConfigureNotices(on ? shell.Api.BaseUrl : "", on ? shell.Api.Token : "",
                                         on ? shell.ChatAccountId : "", on ? shell.Api.Cursors.Current : "",
                                         on);
        }

        /// <summary>Android 13+ 的通知權限：每段 app 行程只問一次（拒了也不再追問）。</summary>
        public static void AskPermissionOnce()
        {
            if (_asked || !NativeShell.Available || NativeShell.Sdk < 33) return;
            _asked = true;
            NativeShell.Request(NativeShell.NotificationPermission, _ => { });
        }

        /// <summary>收到一條 `notice.created`：前景畫提醒條；不在焦點時另響一則系統通知。</summary>
        public static void OnNotice(NextShell shell, RectTransform host, JObject evt)
        {
            var payload = evt?.OptJObject("payload");
            if (payload == null) return;
            var who = payload.OptString("actor_display", payload.OptString("actor_id"));
            var kind = payload.OptString("kind");
            string line;
            if (kind == "reply") line = NextStrings.F("notice_reply", "who", who, "text", payload.OptString("preview"));
            else if (kind == "like") line = NextStrings.F("notice_like", "who", who, "text", payload.OptString("post_preview"));
            else return;
            var postId = payload.OptString("post_id");
            NoticeBar.Show(host, line, () => shell.ShowPost(postId));
            if (!Application.isFocused)
            {
                try { NativeShell.NotifyReminder(who, line); }
                catch (Exception error) { Debug.LogWarning("notice: " + error.Message); }
            }
        }
    }

    /// <summary>
    /// 可以點的提醒條：長得像 <see cref="NextToast"/>（頂欄下緣、置中、膠囊），但**吃點擊**、停得久一點（5 秒），
    /// 點了就開那篇帖並收起。同時只有一條：新的來就換掉舊的。
    /// </summary>
    public sealed class NoticeBar : MonoBehaviour
    {
        private const float FadeIn = 0.15f;
        private const float FadeOut = 0.3f;
        private const float Hold = 5f;

        private CanvasGroup _group;
        private float _born;

        public static void Show(RectTransform host, string text, Action onTap)
        {
            if (host == null || string.IsNullOrWhiteSpace(text)) return;
            var old = host.Find("NextNoticeBar");
            if (old != null) Destroy(old.gameObject);

            var rect = UiBuilder.NewRect("NextNoticeBar", host);
            rect.gameObject.AddComponent<LayoutElement>().ignoreLayout = true;
            rect.anchorMin = rect.anchorMax = new Vector2(0.5f, 1f);
            rect.pivot = new Vector2(0.5f, 1f);
            rect.anchoredPosition = new Vector2(0f, -10f);
            var row = rect.gameObject.AddComponent<HorizontalLayoutGroup>();
            row.padding = new RectOffset(16, 16, 10, 10);
            row.spacing = 8f;
            row.childAlignment = TextAnchor.MiddleCenter;
            row.childControlWidth = true;
            row.childControlHeight = true;
            row.childForceExpandWidth = false;
            row.childForceExpandHeight = false;
            var fit = rect.gameObject.AddComponent<ContentSizeFitter>();
            fit.horizontalFit = ContentSizeFitter.FitMode.PreferredSize;
            fit.verticalFit = ContentSizeFitter.FitMode.PreferredSize;

            var bg = UiBuilder.Backdrop("NextNoticeBarBg", rect, NextTheme.Navy, radius: 16f);
            UiBuilder.Icon("NextNoticeBarIcon", "chat", rect, 14f, NextTheme.OnColor);
            var label = UiBuilder.Text("NextNoticeBarText", rect, text, NextTheme.NoteSize, NextTheme.OnColor,
                                       TextAlignmentOptions.Left, noWrap: true);
            if (label is TMP_Text tmp)
            {
                tmp.raycastTarget = false;
                tmp.overflowMode = TextOverflowModes.Ellipsis;
                var max = Mathf.Max(160f, host.rect.width - 80f);
                if (tmp.GetPreferredValues(text).x > max)
                {
                    var element = tmp.GetComponent<LayoutElement>() ?? tmp.gameObject.AddComponent<LayoutElement>();
                    element.preferredWidth = max;
                }
            }

            var bar = rect.gameObject.AddComponent<NoticeBar>();
            var button = rect.gameObject.AddComponent<Button>();
            button.targetGraphic = bg.GetComponent<Image>();
            button.transition = Selectable.Transition.None;
            button.onClick.AddListener(() =>
            {
                Destroy(rect.gameObject);
                onTap?.Invoke();
            });
            bar._group = rect.gameObject.AddComponent<CanvasGroup>();
            bar._group.alpha = 0f;
            bar._born = Time.realtimeSinceStartup;
            rect.SetAsLastSibling();
        }

        private void Update()
        {
            var age = Time.realtimeSinceStartup - _born;
            if (age >= FadeIn + Hold + FadeOut)
            {
                Destroy(gameObject);
                return;
            }
            if (age < FadeIn) _group.alpha = age / FadeIn;
            else if (age < FadeIn + Hold) _group.alpha = 1f;
            else _group.alpha = 1f - (age - FadeIn - Hold) / FadeOut;
        }
    }
}
