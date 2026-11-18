using BlueAgent.Screens;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>
    /// 一次性的提示（owner 2026-10-30「底部狀態列完全可以不顯示」）：以前「她離席了」「進桌失敗」
    /// 「已複製」這些都寫進底部那條狀態列，而那條也是常駐資訊（身份、thread、連線）——一變就互相蓋掉。
    /// 狀態列拿掉之後，一次性的話改成頂欄下面浮一顆小膠囊，幾秒後自己淡掉；不吃點擊、不佔版面。
    /// 同時只有一顆：新的來就換掉舊的。
    /// </summary>
    public sealed class NextToast : MonoBehaviour
    {
        private const float FadeIn = 0.15f;
        private const float FadeOut = 0.3f;

        private CanvasGroup _group;
        private float _born;
        private float _hold;

        /// <summary>在 <paramref name="host"/> 的上緣置中浮一顆提示；空字串不顯示。</summary>
        public static void Show(RectTransform host, string text, float seconds = 2.6f)
        {
            if (host == null || string.IsNullOrWhiteSpace(text)) return;
            var old = host.Find("NextToast");
            if (old != null) Destroy(old.gameObject);

            var rect = UiBuilder.NewRect("NextToast", host);
            rect.gameObject.AddComponent<LayoutElement>().ignoreLayout = true;
            rect.anchorMin = rect.anchorMax = new Vector2(0.5f, 1f);
            rect.pivot = new Vector2(0.5f, 1f);
            rect.anchoredPosition = new Vector2(0f, -10f);
            var row = rect.gameObject.AddComponent<HorizontalLayoutGroup>();
            row.padding = new RectOffset(16, 16, 9, 9);
            row.childAlignment = TextAnchor.MiddleCenter;
            row.childControlWidth = true;
            row.childControlHeight = true;
            row.childForceExpandWidth = false;
            row.childForceExpandHeight = false;
            var fit = rect.gameObject.AddComponent<ContentSizeFitter>();
            fit.horizontalFit = ContentSizeFitter.FitMode.PreferredSize;
            fit.verticalFit = ContentSizeFitter.FitMode.PreferredSize;

            var bg = UiBuilder.Backdrop("NextToastBg", rect, new Color(NextTheme.Navy.r, NextTheme.Navy.g,
                                                                       NextTheme.Navy.b, 0.92f), radius: 16f);
            bg.GetComponent<Image>().raycastTarget = false;
            var label = UiBuilder.Text("NextToastText", rect, text, NextTheme.NoteSize, NextTheme.OnColor,
                                       TextAlignmentOptions.Center, noWrap: true);
            if (label is TMP_Text tmp)
            {
                tmp.raycastTarget = false;
                // 太長就換行，最寬到畫面寬減兩邊留白
                var max = Mathf.Max(160f, host.rect.width - 48f);
                if (tmp.GetPreferredValues(text).x > max)
                {
                    tmp.textWrappingMode = TextWrappingModes.Normal;
                    var element = tmp.GetComponent<LayoutElement>() ?? tmp.gameObject.AddComponent<LayoutElement>();
                    element.preferredWidth = max;
                }
            }

            var toast = rect.gameObject.AddComponent<NextToast>();
            toast._group = rect.gameObject.AddComponent<CanvasGroup>();
            toast._group.blocksRaycasts = false;
            toast._group.interactable = false;
            toast._group.alpha = 0f;
            toast._born = Time.realtimeSinceStartup;
            toast._hold = Mathf.Max(0.5f, seconds);
            rect.SetAsLastSibling();
        }

        private void Update()
        {
            var age = Time.realtimeSinceStartup - _born;
            if (age >= FadeIn + _hold + FadeOut)
            {
                Destroy(gameObject);
                return;
            }
            if (age < FadeIn) _group.alpha = age / FadeIn;
            else if (age < FadeIn + _hold) _group.alpha = 1f;
            else _group.alpha = 1f - (age - FadeIn - _hold) / FadeOut;
        }
    }
}
