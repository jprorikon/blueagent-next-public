using BlueAgent.Next;
using BlueAgent.Screens;
using UnityEngine;
using UnityEngine.UI;

namespace BlueAgent.MR
{
    /// <summary>
    /// 通話中她頭頂的聽筒圖示（owner 2026-11-01：「非通話時什麼都不顯示，通話狀態時角色頭頂顯示聽筒圖標」）。
    ///
    /// 一顆圓形綠底（`NextTheme.Ok`）＋白色聽筒（手機那套烤好的 `i-phone`）；接通中＝實心，撥號中＝一明一暗。
    /// 擺在她頭頂、永遠轉向你（跟頭頂氣泡同一個轉法）；她說話時氣泡疊在圖示上方（<see cref="Height"/>）。
    /// </summary>
    public sealed class MrCallBadge : MonoBehaviour
    {
        /// <summary>1 pt 是幾公尺（跟 `MrBubble` 同一個比例：她 30 cm 高，氣泡最寬 26 cm）。</summary>
        private const float MetersPerPoint = 0.26f / 312f;
        private const float Size = 30f;
        private const float IconSize = 18f;
        /// <summary>圖示底在她頭頂上方多高（公尺）。</summary>
        private const float Above = 0.02f;
        /// <summary>這顆圖示佔掉的高度（公尺；頭頂氣泡要往上讓這麼多）。</summary>
        public const float Height = Above + Size * MetersPerPoint;

        private CanvasGroup _group;

        public static MrCallBadge Create(Camera eye)
        {
            var go = new GameObject("MR Call Badge", typeof(RectTransform));
            var badge = go.AddComponent<MrCallBadge>();
            var canvas = go.AddComponent<Canvas>();
            canvas.renderMode = RenderMode.WorldSpace;
            canvas.worldCamera = eye;
            var root = (RectTransform)go.transform;
            root.sizeDelta = new Vector2(Size, Size);
            root.pivot = new Vector2(0.5f, 0f);
            root.localScale = Vector3.one * MetersPerPoint;
            badge._group = go.AddComponent<CanvasGroup>();

            var disc = new GameObject("Disc", typeof(RectTransform)).GetComponent<RectTransform>();
            disc.SetParent(root, false);
            disc.anchorMin = Vector2.zero;
            disc.anchorMax = Vector2.one;
            disc.offsetMin = disc.offsetMax = Vector2.zero;
            var image = disc.gameObject.AddComponent<Image>();
            image.color = NextTheme.Ok;
            image.raycastTarget = false;
            UiBuilder.RoundCorners(image, Size * 0.5f, Size * 0.5f, Size * 0.5f, Size * 0.5f);
            var drop = disc.gameObject.AddComponent<Shadow>();
            drop.effectColor = NextTheme.Shadow;
            drop.effectDistance = new Vector2(0f, -1.5f);
            UiBuilder.Icon("Phone", "phone", disc, IconSize, NextTheme.OnColor);

            go.SetActive(false);
            return badge;
        }

        /// <summary>
        /// 這一幀：<paramref name="inCall"/> false ⇒ 收起來；不然擺在 <paramref name="headTop"/> 上方、轉向 <paramref name="eye"/>。
        /// <paramref name="live"/> false（還在撥）⇒ 一明一暗。
        /// </summary>
        public void Show(bool inCall, bool live, Vector3 headTop, Vector3 eye)
        {
            if (!inCall)
            {
                if (gameObject.activeSelf) gameObject.SetActive(false);
                return;
            }
            _group.alpha = live ? 1f : 0.45f + 0.55f * Mathf.Abs(Mathf.Sin(Time.unscaledTime * 3f));
            transform.position = headTop + Vector3.up * Above;
            var toEye = Vector3.ProjectOnPlane(transform.position - eye, Vector3.up);
            if (toEye.sqrMagnitude > 1e-6f) transform.rotation = Quaternion.LookRotation(toEye, Vector3.up);
            if (!gameObject.activeSelf) gameObject.SetActive(true);
        }
    }
}
