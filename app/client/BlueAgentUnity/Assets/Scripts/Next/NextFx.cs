using System;
using TMPro;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>
    /// 平行四邊形：把圖形的頂點按高度水平錯開（上緣往右、下緣往左）。原創幾何風格按鈕／徽章／頁籤的底板都靠它。
    /// 只錯開底板那張圖；字在另一個物件上，不跟著斜。
    /// 錯開量＝高度 × <see cref="Amount"/> ⇒ 左右各多出 `高/2 × Amount`，排版時要在兩側留一點空。
    /// </summary>
    [RequireComponent(typeof(Graphic))]
    public sealed class NextSkew : BaseMeshEffect
    {
        public float Amount = NextTheme.Skew;

        public override void ModifyMesh(VertexHelper mesh)
        {
            if (!IsActive() || mesh.currentVertCount == 0) return;
            var centerY = graphic.rectTransform.rect.center.y;
            var vertex = new UIVertex();
            for (var i = 0; i < mesh.currentVertCount; i++)
            {
                mesh.PopulateUIVertex(ref vertex, i);
                vertex.position.x += (vertex.position.y - centerY) * Amount;
                mesh.SetUIVertex(vertex, i);
            }
        }
    }

    /// <summary>
    /// 上下漸層：每個頂點照它的高度在 <see cref="Bottom"/>→<see cref="Top"/> 之間取色（乘上圖形自己的 alpha）。
    /// 圓角 sprite（九宮格）頂點多，照樣是平滑的。
    /// </summary>
    [RequireComponent(typeof(Graphic))]
    public sealed class NextGradient : BaseMeshEffect
    {
        public Color Top = Color.white;
        public Color Bottom = Color.white;

        public override void ModifyMesh(VertexHelper mesh)
        {
            if (!IsActive() || mesh.currentVertCount == 0) return;
            var rect = graphic.rectTransform.rect;
            var height = Mathf.Max(1f, rect.height);
            var vertex = new UIVertex();
            for (var i = 0; i < mesh.currentVertCount; i++)
            {
                mesh.PopulateUIVertex(ref vertex, i);
                var t = Mathf.Clamp01((vertex.position.y - rect.yMin) / height);
                var color = Color.Lerp(Bottom, Top, t);
                color.a *= vertex.color.a / 255f;
                vertex.color = color;
                mesh.SetUIVertex(vertex, i);
            }
        }
    }

    /// <summary>
    /// 氣泡寬度跟字走（MomoTalk 那樣：一句短話就是一顆小泡泡），最寬 <see cref="Max"/>。
    /// 字會在串流中一直變 ⇒ 每次字變了才重算（LateUpdate 比對），不是只在建立時算一次。
    /// </summary>
    public sealed class NextFitWidth : MonoBehaviour
    {
        public TMP_Text Text;
        public float Max = 312f;
        public float Min = 48f;
        /// <summary>左右內距的和（泡泡的 padding）。</summary>
        public float Pad = 24f;
        private string _last;
        private LayoutElement _layout;

        private void LateUpdate()
        {
            if (Text == null) return;
            var content = Text.text;
            if (content == _last) return;
            _last = content;
            var wanted = Text.GetPreferredValues(content, Max - Pad, 0f).x + Pad + 2f;
            var width = Mathf.Clamp(wanted, Min, Max);
            if (_layout == null) _layout = GetComponent<LayoutElement>() ?? gameObject.AddComponent<LayoutElement>();
            _layout.minWidth = width;
            _layout.preferredWidth = width;
            var parent = transform.parent as RectTransform;
            LayoutRebuilder.MarkLayoutForRebuild(parent != null ? parent : (RectTransform)transform);
        }
    }

    /// <summary>
    /// 下拉更新（owner 2026-11-08：公共区拿掉標題＋重新整理那一排，改下拉）。
    /// 掛在 ScrollRect 同一個物件上：拖動結束時內容被往下拉超過 <see cref="Threshold"/> 就呼叫
    /// <see cref="OnRefresh"/>；拉的過程中頂上那行提示跟著淡入、文字換成「放開更新」。
    /// ScrollRect 要是 Elastic 才拉得動（Clamped 到頂就停住）。
    /// </summary>
    public sealed class NextPullRefresh : MonoBehaviour, IBeginDragHandler, IEndDragHandler
    {
        public ScrollRect Scroll;
        public Action OnRefresh;
        public float Threshold = 64f;
        public TMP_Text Hint;
        public string PullText = "";
        public string ReleaseText = "";
        private bool _dragging;

        public void OnBeginDrag(PointerEventData eventData) { _dragging = true; }

        public void OnEndDrag(PointerEventData eventData)
        {
            _dragging = false;
            if (Scroll != null && Scroll.content != null && Pull() >= Threshold) OnRefresh?.Invoke();
        }

        private float Pull()
        {
            // 內容頂端對齊視窗頂端時 y=0；往下拉超過頂端 ⇒ y 變負
            return Scroll != null && Scroll.content != null ? -Scroll.content.anchoredPosition.y : 0f;
        }

        private void LateUpdate()
        {
            if (Hint == null) return;
            var pull = Pull();
            var ready = _dragging && pull >= Threshold;
            Hint.alpha = Mathf.Clamp01(pull / Threshold);
            var text = ready ? ReleaseText : PullText;
            if (Hint.text != text) Hint.text = text;
        }
    }
}
