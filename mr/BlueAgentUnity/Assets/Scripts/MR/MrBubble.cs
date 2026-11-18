using BlueAgent.Next;
using BlueAgent.Screens;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

namespace BlueAgent.MR
{
    /// <summary>
    /// 她頭頂的對話氣泡（owner 2026-11-01：「顯示成她頭頂的氣泡」→「風格參照手機 app」）。
    ///
    /// 照手機聊天頁她那一顆（`NextChatView.BuildBubble` 的 Hers）：底色 `NextTheme.HerBubble`、白字、
    /// 她的日語字型、正文 15、內距 12／12／8／8、最寬 312、圓角 12 ＋ 一個 3 的尖角、淡陰影。
    /// 手機上尖角在左上（指向左邊的頭像）；頭頂上指向她 ⇒ 改在**左下**。
    ///
    /// 一張世界空間的小畫布（1 單位＝手機上的 1 pt，再整體縮到公尺）。手機那支 `UiBuilder.Text` 要靠聊天頁的
    /// 排版回合量字高，這裡沒有那一套 ⇒ 大小用 TMP 自己量（`GetPreferredValues`），字一換才重算。
    /// 永遠轉向你（只轉左右，字不跟著你低頭仰頭歪掉）。
    /// </summary>
    public sealed class MrBubble : MonoBehaviour
    {
        /// <summary>手機上的 1 pt 在這裡是幾公尺：最寬 312 pt ≈ 26 cm（她 30 cm 高）。</summary>
        private const float MetersPerPoint = 0.26f / MaxWidth;
        private const float MaxWidth = 312f;
        private const float MinWidth = 48f;
        private const float PadX = 12f;
        private const float PadY = 8f;
        /// <summary>氣泡底在她頭頂上方多高（公尺）。</summary>
        private const float Above = 0.04f;
        /// <summary>最多顯示幾個字（太長只留最後一段；server 本來就把話切成每段 ≤ 3 句）。</summary>
        private const int MaxChars = 60;

        private RectTransform _panel;
        private TextMeshProUGUI _text;
        private string _shown;

        public static MrBubble Create(Camera eye)
        {
            var go = new GameObject("MR Bubble", typeof(RectTransform));
            var bubble = go.AddComponent<MrBubble>();
            var canvas = go.AddComponent<Canvas>();
            canvas.renderMode = RenderMode.WorldSpace;
            canvas.worldCamera = eye;
            var root = (RectTransform)go.transform;
            root.sizeDelta = new Vector2(MaxWidth, 200f);
            root.pivot = new Vector2(0.5f, 0f);
            root.localScale = Vector3.one * MetersPerPoint;

            // 底：圓角色塊（左下那個角是尖的，指向她）＋ 手機同一個淡陰影
            var panel = new GameObject("Hers", typeof(RectTransform)).GetComponent<RectTransform>();
            panel.SetParent(root, false);
            panel.anchorMin = panel.anchorMax = new Vector2(0.5f, 0f);
            panel.pivot = new Vector2(0.5f, 0f);
            var image = panel.gameObject.AddComponent<Image>();
            image.color = NextTheme.HerBubble;
            image.raycastTarget = false;
            UiBuilder.RoundCorners(image, 12f, 12f, 12f, 3f);
            var drop = panel.gameObject.AddComponent<Shadow>();
            drop.effectColor = NextTheme.Shadow;
            drop.effectDistance = new Vector2(0f, -2f);

            var label = new GameObject("BubbleText", typeof(RectTransform)).GetComponent<RectTransform>();
            label.SetParent(panel, false);
            label.anchorMin = Vector2.zero;
            label.anchorMax = Vector2.one;
            label.offsetMin = new Vector2(PadX, PadY);
            label.offsetMax = new Vector2(-PadX, -PadY);
            var text = label.gameObject.AddComponent<TextMeshProUGUI>();
            text.font = NextFonts.Her;                                       // 她說的是日語：日語字形
            text.fontSize = NextTheme.BodySize;
            text.color = NextTheme.OnColor;
            text.alignment = TextAlignmentOptions.TopLeft;
            text.textWrappingMode = TextWrappingModes.Normal;
            text.richText = false;                                           // 她的話原樣顯示，裡面的 < 不當標籤
            text.raycastTarget = false;

            bubble._panel = panel;
            bubble._text = text;
            go.SetActive(false);
            return bubble;
        }

        /// <summary>這一幀：<paramref name="line"/> 空＝收起來；不然擺在 <paramref name="headTop"/> 上方、轉向 <paramref name="eye"/>。</summary>
        public void Show(string line, Vector3 headTop, Vector3 eye)
        {
            if (string.IsNullOrEmpty(line))
            {
                if (gameObject.activeSelf) gameObject.SetActive(false);
                _shown = null;
                return;
            }
            if (line.Length > MaxChars) line = "…" + line.Substring(line.Length - MaxChars);
            if (line != _shown)
            {
                _text.text = line;
                _shown = line;
                // 手機的 NextFitWidth：短的話氣泡跟著字縮，長的話到 312 就換行
                var natural = _text.GetPreferredValues(line, MaxWidth - PadX * 2f, 0f);
                var width = Mathf.Clamp(natural.x + PadX * 2f, MinWidth, MaxWidth);
                var height = _text.GetPreferredValues(line, width - PadX * 2f, 0f).y + PadY * 2f;
                _panel.sizeDelta = new Vector2(width, height);
            }
            transform.position = headTop + Vector3.up * Above;
            var toEye = Vector3.ProjectOnPlane(transform.position - eye, Vector3.up);
            if (toEye.sqrMagnitude > 1e-6f) transform.rotation = Quaternion.LookRotation(toEye, Vector3.up);
            if (!gameObject.activeSelf) gameObject.SetActive(true);
        }
    }
}
