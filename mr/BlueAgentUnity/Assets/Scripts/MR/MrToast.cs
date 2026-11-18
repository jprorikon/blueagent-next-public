using System.Collections.Generic;
using BlueAgent.Next;
using BlueAgent.Screens;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

namespace BlueAgent.MR
{
    /// <summary>
    /// 視野下方的事件通知（owner 2026-11-01：「debug 用的顯示轉移到 UI 上，事件通知放在視野下方，超時消失」）。
    ///
    /// 一張跟著頭的世界空間小畫布（慢半拍跟、永遠面向你，跟儀表同一個作法）；每則通知一顆深色圓角膠囊、白字，
    /// 最新的在最下面，最多同時 <see cref="MaxShown"/> 則；每則 <see cref="Seconds"/> 秒後淡出。
    /// 同一句話還掛著時又來一次 ⇒ 只把它的計時重來（不疊兩顆一樣的）。
    /// 只放「發生了什麼」（連上／斷了、通話怎麼結束、換了誰、放不下去…）；量測數字留在除錯儀表（`MrProbe`，預設藏起來）。
    /// </summary>
    public sealed class MrToast : MonoBehaviour
    {
        /// <summary>一則留多久（秒，不含淡出）。</summary>
        private const float Seconds = 4f;
        private const float FadeSeconds = 0.5f;
        private const int MaxShown = 3;
        /// <summary>在眼前多遠、往視野下緣偏多少（公尺；1 m 外 0.3 m ≈ 往下 17°，Quest 3 視野上下約 ±45°）。</summary>
        private const float Distance = 1.0f;
        private const float Drop = 0.3f;
        /// <summary>1 pt 是幾公尺（1 m 外 15 pt 的字約 1.5 cm 高，跟手機拿在手上看起來差不多）。</summary>
        private const float MetersPerPoint = 0.001f;
        private const float MaxWidth = 420f;
        private const float PadX = 14f;
        private const float PadY = 8f;
        private const float Gap = 6f;

        private sealed class Item
        {
            public string Text;
            public RectTransform Rect;
            public CanvasGroup Group;
            public float Until;
        }

        private Camera _camera;
        private RectTransform _root;
        private readonly List<Item> _items = new List<Item>();

        public static MrToast Create(Camera eye)
        {
            var go = new GameObject("MR Toast", typeof(RectTransform));
            var toast = go.AddComponent<MrToast>();
            var canvas = go.AddComponent<Canvas>();
            canvas.renderMode = RenderMode.WorldSpace;
            canvas.worldCamera = eye;
            canvas.sortingOrder = 10;
            toast._camera = eye;
            toast._root = (RectTransform)go.transform;
            toast._root.sizeDelta = new Vector2(MaxWidth, 300f);
            toast._root.pivot = new Vector2(0.5f, 0f);                 // 底邊對齊那一點，往上疊
            toast._root.localScale = Vector3.one * MetersPerPoint;
            return toast;
        }

        /// <summary>來一則通知（已經是這一種語言的字）。空字串不理。</summary>
        public void Post(string text)
        {
            if (string.IsNullOrEmpty(text)) return;
            Debug.Log("[mr] 通知：" + text);
            var now = Time.unscaledTime;
            foreach (var item in _items)
            {
                if (item.Text != text) continue;
                item.Until = now + Seconds;                            // 同一句還掛著：重新計時就好
                return;
            }
            _items.Add(Build(text, now + Seconds));
            while (_items.Count > MaxShown)
            {
                Destroy(_items[0].Rect.gameObject);
                _items.RemoveAt(0);
            }
            if (_camera != null) Snap();
        }

        private Item Build(string text, float until)
        {
            var pill = new GameObject("Toast", typeof(RectTransform)).GetComponent<RectTransform>();
            pill.SetParent(_root, false);
            pill.anchorMin = pill.anchorMax = new Vector2(0.5f, 0f);
            pill.pivot = new Vector2(0.5f, 0f);
            var group = pill.gameObject.AddComponent<CanvasGroup>();
            var image = pill.gameObject.AddComponent<Image>();
            var ink = NextTheme.Ink;
            image.color = new Color(ink.r, ink.g, ink.b, 0.88f);       // passthrough 上什麼背景都有 ⇒ 深底白字
            image.raycastTarget = false;
            UiBuilder.RoundCorners(image, 14f, 14f, 14f, 14f);

            var label = new GameObject("ToastText", typeof(RectTransform)).GetComponent<RectTransform>();
            label.SetParent(pill, false);
            label.anchorMin = Vector2.zero;
            label.anchorMax = Vector2.one;
            label.offsetMin = new Vector2(PadX, PadY);
            label.offsetMax = new Vector2(-PadX, -PadY);
            var tmp = label.gameObject.AddComponent<TextMeshProUGUI>();
            tmp.font = NextFonts.Ui;
            tmp.fontSize = NextTheme.BodySize;
            tmp.color = NextTheme.OnColor;
            tmp.alignment = TextAlignmentOptions.Center;
            tmp.textWrappingMode = TextWrappingModes.Normal;
            tmp.richText = false;
            tmp.raycastTarget = false;
            tmp.text = text;
            // 短的跟著字縮，長的到 MaxWidth 換行（跟頭頂氣泡同一個量法）
            var natural = tmp.GetPreferredValues(text, MaxWidth - PadX * 2f, 0f);
            var width = Mathf.Min(natural.x + PadX * 2f, MaxWidth);
            var height = tmp.GetPreferredValues(text, width - PadX * 2f, 0f).y + PadY * 2f;
            pill.sizeDelta = new Vector2(width, height);
            return new Item { Text = text, Rect = pill, Group = group, Until = until };
        }

        private void LateUpdate()
        {
            var now = Time.unscaledTime;
            for (var i = _items.Count - 1; i >= 0; i--)
            {
                var item = _items[i];
                var left = item.Until - now;
                if (left <= -FadeSeconds)
                {
                    Destroy(item.Rect.gameObject);
                    _items.RemoveAt(i);
                    continue;
                }
                item.Group.alpha = left >= 0f ? 1f : 1f + left / FadeSeconds;
            }
            // 最新的在最下面，舊的往上疊
            var y = 0f;
            for (var i = _items.Count - 1; i >= 0; i--)
            {
                _items[i].Rect.anchoredPosition = new Vector2(0f, y);
                y += _items[i].Rect.sizeDelta.y + Gap;
            }
            if (_items.Count > 0) Follow(Time.unscaledDeltaTime);
        }

        /// <summary>跟著頭、慢半拍（轉頭時看得到它滑過來，不鎖死在臉上）。</summary>
        private void Follow(float dt)
        {
            var head = _camera.transform;
            var target = Target(head);
            var t = 1f - Mathf.Exp(-4f * dt);
            transform.position = Vector3.Lerp(transform.position, target, t);
            transform.rotation = Quaternion.LookRotation(transform.position - head.position, Vector3.up);
        }

        /// <summary>沒有通知時不跟；新的一則出現時直接就位（不要從上次的位置滑過來）。</summary>
        private void Snap()
        {
            if (_items.Count != 1) return;
            var head = _camera.transform;
            transform.position = Target(head);
            transform.rotation = Quaternion.LookRotation(transform.position - head.position, Vector3.up);
        }

        /// <summary>視野正下方：沿著你看的方向、往你頭頂的「下」偏（低頭看她時也還在視野下緣，不會飛到頭上）。</summary>
        private Vector3 Target(Transform head)
        {
            return head.position + head.forward * Distance - head.up * Drop;
        }
    }
}
