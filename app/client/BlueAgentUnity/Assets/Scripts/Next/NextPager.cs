using System;
using System.Collections.Generic;
using BlueAgent.Screens;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>
    /// 橫向分頁（owner 2026-11-03「＋ 號選單做成兩排、可以橫向滑動換頁」）：一頁＝可視區那麼寬，
    /// 放手就吸到最近的一頁（甩得快就往甩的方向翻一頁），下面一排小圓點標第幾頁。
    ///
    /// 掛在 `ScrollRect` 同一個節點上（拖曳事件兩邊都收得到）。頁寬跟著可視區走：
    /// 選單是開合動畫、聊天頁大螢幕又分兩欄，建頁那一刻量不到真寬 ⇒ 每幀看一次，寬變了才重排。
    /// </summary>
    public sealed class NextPager : MonoBehaviour, IBeginDragHandler, IEndDragHandler
    {
        /// <summary>甩動速度超過這個（px/s）就照方向翻頁，不看停在哪。</summary>
        private const float FlickSpeed = 300f;

        public ScrollRect Scroll;
        public RectTransform Viewport;
        public readonly List<LayoutElement> Pages = new List<LayoutElement>();
        public readonly List<Image> Dots = new List<Image>();

        private float _width = -1f;
        private int _page;
        private float? _target;

        public int Page => _page;

        private void LateUpdate()
        {
            if (Scroll == null || Viewport == null) return;
            var width = Viewport.rect.width;
            if (width > 1f && !Mathf.Approximately(width, _width))
            {
                _width = width;
                foreach (var page in Pages)
                {
                    if (page == null) continue;
                    page.minWidth = width;
                    page.preferredWidth = width;
                }
                LayoutRebuilder.MarkLayoutForRebuild(Scroll.content);
                SnapTo(_page, true);
            }
            if (!_target.HasValue) return;
            var goal = _target.Value;
            var position = Mathf.Lerp(Scroll.horizontalNormalizedPosition, goal,
                                      1f - Mathf.Exp(-18f * Time.unscaledDeltaTime));
            if (Mathf.Abs(position - goal) < 0.002f)
            {
                position = goal;
                _target = null;
            }
            Scroll.horizontalNormalizedPosition = position;
        }

        public void OnBeginDrag(PointerEventData eventData)
        {
            _target = null;                                // 手指按上去就停下吸附動畫
        }

        public void OnEndDrag(PointerEventData eventData)
        {
            var count = Pages.Count;
            if (count <= 1 || Scroll == null) return;
            var exact = Mathf.Clamp01(Scroll.horizontalNormalizedPosition) * (count - 1);
            var page = Mathf.RoundToInt(exact);
            var speed = Scroll.velocity.x;                 // 內容往左走（負）＝看下一頁
            if (Mathf.Abs(speed) > FlickSpeed)
                page = speed < 0f ? Mathf.CeilToInt(exact) : Mathf.FloorToInt(exact);
            SnapTo(page, false);
        }

        /// <summary>翻到第 <paramref name="page"/> 頁（0 起算）。</summary>
        public void SnapTo(int page, bool instant)
        {
            var count = Pages.Count;
            _page = Mathf.Clamp(page, 0, Mathf.Max(0, count - 1));
            if (Scroll != null)
            {
                Scroll.velocity = Vector2.zero;
                var goal = count > 1 ? _page / (float)(count - 1) : 0f;
                if (instant)
                {
                    Scroll.horizontalNormalizedPosition = goal;
                    _target = null;
                }
                else
                {
                    _target = goal;
                }
            }
            for (var i = 0; i < Dots.Count; i++)
                if (Dots[i] != null) Dots[i].color = i == _page ? NextTheme.Sky : NextTheme.Idle;
        }

        /// <summary>
        /// 在 <paramref name="parent"/> 裡建一個橫向分頁區：回傳 pager，頁面用 <see cref="AddPage"/> 加。
        /// 結構：host（吃版面）→ viewport（遮罩）→ content（橫排、照內容寬）→ 每一頁。
        /// </summary>
        public static NextPager Create(string name, RectTransform parent)
        {
            var host = UiBuilder.NewRect(name, parent);
            UiBuilder.Weight(host, 1f);
            var viewport = UiBuilder.NewRect(name + "Viewport", host);
            viewport.gameObject.AddComponent<LayoutElement>().ignoreLayout = true;
            UiBuilder.Stretch(viewport);
            viewport.gameObject.AddComponent<RectMask2D>();
            viewport.gameObject.AddComponent<Image>().color = new Color(1f, 1f, 1f, 0f);   // 空白處也吃拖曳

            var content = UiBuilder.Row(name + "Content", viewport, 0f, null, TextAnchor.UpperLeft, false);
            content.anchorMin = new Vector2(0f, 0f);
            content.anchorMax = new Vector2(0f, 1f);
            content.pivot = new Vector2(0f, 1f);
            content.anchoredPosition = Vector2.zero;
            content.sizeDelta = new Vector2(0f, 0f);
            content.gameObject.AddComponent<ContentSizeFitter>().horizontalFit =
                ContentSizeFitter.FitMode.PreferredSize;

            var scroll = host.gameObject.AddComponent<ScrollRect>();
            scroll.content = content;
            scroll.viewport = viewport;
            scroll.horizontal = true;
            scroll.vertical = false;
            scroll.movementType = ScrollRect.MovementType.Elastic;
            scroll.inertia = true;
            scroll.decelerationRate = 0.05f;
            scroll.scrollSensitivity = 0f;                 // 滑鼠滾輪不翻頁（桌面上會跟對話區搶滾輪）

            var pager = host.gameObject.AddComponent<NextPager>();
            pager.Scroll = scroll;
            pager.Viewport = viewport;
            return pager;
        }

        /// <summary>加一頁（直排容器；寬度由 pager 每幀對齊可視區）。</summary>
        public RectTransform AddPage(string name, float spacing)
        {
            var page = UiBuilder.Column(name, Scroll.content, spacing, null, true, TextAnchor.UpperLeft);
            var element = page.GetComponent<LayoutElement>() ?? page.gameObject.AddComponent<LayoutElement>();
            element.flexibleWidth = 0f;
            Pages.Add(element);
            return page;
        }

        /// <summary>頁數 &gt; 1 才畫的小圓點列（放在 <paramref name="parent"/> 裡、pager 下面）。</summary>
        public void BuildDots(string name, RectTransform parent)
        {
            if (Pages.Count <= 1) return;
            var row = UiBuilder.Row(name, parent, 6f, null, TextAnchor.MiddleCenter);
            UiBuilder.FixedHeight(row, 8f);
            for (var i = 0; i < Pages.Count; i++)
            {
                var dot = UiBuilder.NewRect(name + i, row);
                UiBuilder.FixedHeight(dot, 6f);
                UiBuilder.FixedWidth(dot, 6f);
                var image = dot.gameObject.AddComponent<Image>();
                UiBuilder.Round(image, 3f);
                image.raycastTarget = false;
                Dots.Add(image);
            }
            SnapTo(_page, true);
        }
    }
}
