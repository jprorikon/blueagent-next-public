using System;
using System.Collections.Generic;
using BlueAgent.Screens;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>
    /// Next 的動態（2026-10-29 原創幾何風格改版）：一支常駐的補間器＋幾個小元件。
    ///
    /// 動的東西只碰 **alpha、縮放、旋轉、不受版面管的疊層位置**——版面（LayoutGroup）管的位置不去動，
    /// 免得版面一重算就跳回去。時間用 `unscaledDeltaTime`（不受暫停影響）。
    /// 補間綁著一個擁有者：擁有者被銷毀（換頁）就自己丟掉，不會對已死的物件寫值。
    /// </summary>
    public sealed class NextMotion : MonoBehaviour
    {
        private sealed class Tween
        {
            public UnityEngine.Object Owner;
            public float Delay;
            public float Duration;
            public float Elapsed;
            public Action<float> Apply;
            public Func<float, float> Ease;
            public Action Done;
        }

        private static NextMotion _runner;
        private readonly List<Tween> _tweens = new List<Tween>();

        /// <summary>補間單幀最多推進的秒數（見 `Update`）。</summary>
        private const float MaxStep = 1f / 20f;

        private static NextMotion Runner
        {
            get
            {
                if (_runner != null) return _runner;
                var go = new GameObject("NextMotion");
                DontDestroyOnLoad(go);
                _runner = go.AddComponent<NextMotion>();
                return _runner;
            }
        }

        // ── 緩動 ──
        public static float OutCubic(float t) { t = 1f - t; return 1f - t * t * t; }
        public static float InOutSine(float t) => -(Mathf.Cos(Mathf.PI * t) - 1f) / 2f;
        public static float OutBack(float t)
        {
            const float c1 = 1.70158f;
            const float c3 = c1 + 1f;
            t -= 1f;
            return 1f + c3 * t * t * t + c1 * t * t;
        }

        /// <summary>補一段（0→1 的進度交給 <paramref name="apply"/>）。</summary>
        public static void Play(UnityEngine.Object owner, float seconds, Action<float> apply, float delay = 0f,
                                Func<float, float> ease = null, Action done = null)
        {
            if (owner == null || apply == null) return;
            apply((ease ?? OutCubic)(0f));
            Runner._tweens.Add(new Tween
            {
                Owner = owner, Delay = delay, Duration = Mathf.Max(0.0001f, seconds),
                Apply = apply, Ease = ease ?? OutCubic, Done = done,
            });
        }

        private void Update()
        {
            // 換頁那一幀要建整頁（聊天頁還要載 3D 立繪）常常卡 0.5 秒以上；照實際 dt 推進的話，
            // 0.42 秒的掃光與淡入在下一幀就直接跑完、看不到（2026-11-07）。單幀最多推 1/20 秒：
            // 卡頓之後動畫照樣完整播完，正常幀率（≥20fps）時速度不變。
            var dt = Mathf.Min(Time.unscaledDeltaTime, MaxStep);
            for (var i = _tweens.Count - 1; i >= 0; i--)
            {
                var tween = _tweens[i];
                if (tween.Owner == null) { _tweens.RemoveAt(i); continue; }
                if (tween.Delay > 0f) { tween.Delay -= dt; continue; }
                tween.Elapsed += dt;
                var p = Mathf.Clamp01(tween.Elapsed / tween.Duration);
                tween.Apply(tween.Ease(p));
                if (p < 1f) continue;
                _tweens.RemoveAt(i);
                tween.Done?.Invoke();
            }
        }

        // ── 常用的幾種 ──

        /// <summary>淡入＋從 96% 放大（頁面、卡片進場）。<paramref name="delay"/> 用來錯開（一張接一張）。</summary>
        public static void Enter(RectTransform rect, float delay = 0f)
        {
            if (rect == null) return;
            var group = rect.GetComponent<CanvasGroup>() ?? rect.gameObject.AddComponent<CanvasGroup>();
            Play(rect, NextTheme.EnterSeconds, p =>
            {
                group.alpha = p;
                var s = Mathf.Lerp(0.96f, 1f, p);
                rect.localScale = new Vector3(s, s, 1f);
            }, delay);
        }

        /// <summary>一欄裡的子項一張接一張進場（設定頁的卡片、相簿的入口）。</summary>
        public static void Stagger(RectTransform column, int skip = 0)
        {
            if (column == null) return;
            for (var i = skip; i < column.childCount; i++)
            {
                if (column.GetChild(i) is RectTransform child && child.GetComponent<LayoutElement>()?.ignoreLayout != true)
                    Enter(child, (i - skip) * NextTheme.StaggerSeconds);
            }
        }

        /// <summary>
        /// 換頁時的斜向掃光：三條斜帶（天藍／白／黃）從左掃到右，0.4 秒（BA 的轉場語言，原創畫法）。
        /// 疊在整張畫布上、不吃點擊、掃完自己消失。
        /// </summary>
        public static void Wipe(RectTransform canvas)
        {
            if (canvas == null) return;
            var host = UiBuilder.NewRect("NextWipe", canvas);
            host.gameObject.AddComponent<LayoutElement>().ignoreLayout = true;
            UiBuilder.Stretch(host);
            host.SetAsLastSibling();
            var width = canvas.rect.width;
            var bands = new[]
            {
                (NextTheme.Sky, 120f, 0.30f), (Color.white, 46f, 0.55f), (NextTheme.Yellow, 14f, 0.65f),
            };
            var offset = 0f;
            foreach (var (color, size, alpha) in bands)
            {
                var band = UiBuilder.NewRect("Band", host);
                band.anchorMin = new Vector2(0f, 0f);
                band.anchorMax = new Vector2(0f, 1f);
                band.pivot = new Vector2(1f, 0.5f);
                band.sizeDelta = new Vector2(size, 0f);
                var image = band.gameObject.AddComponent<Image>();
                image.color = new Color(color.r, color.g, color.b, alpha);
                image.raycastTarget = false;
                band.gameObject.AddComponent<NextSkew>().Amount = 0.35f;
                var lead = offset;
                Play(band, 0.42f, p =>
                {
                    band.anchoredPosition = new Vector2(Mathf.Lerp(-lead, width + 260f + size - lead, p), 0f);
                }, 0f, InOutSine);
                offset += size + 10f;
            }
            Play(host, 0.45f, _ => { }, 0f, null, () => { if (host != null) Destroy(host.gameObject); });
        }
    }

    /// <summary>按下縮一下、放開彈回來（所有按鈕都掛）。</summary>
    public sealed class NextPress : MonoBehaviour, IPointerDownHandler, IPointerUpHandler, IPointerExitHandler
    {
        private bool _down;

        public void OnPointerDown(PointerEventData eventData)
        {
            _down = true;
            var rect = (RectTransform)transform;
            var from = rect.localScale.x;
            NextMotion.Play(this, 0.08f, p =>
            {
                var s = Mathf.Lerp(from, NextTheme.PressScale, p);
                rect.localScale = new Vector3(s, s, 1f);
            });
        }

        public void OnPointerUp(PointerEventData eventData) => Release();
        public void OnPointerExit(PointerEventData eventData) { if (_down) Release(); }

        private void Release()
        {
            _down = false;
            var rect = (RectTransform)transform;
            var from = rect.localScale.x;
            NextMotion.Play(this, 0.28f, p =>
            {
                var s = Mathf.LerpUnclamped(from, 1f, p);
                rect.localScale = new Vector3(s, s, 1f);
            }, 0f, NextMotion.OutBack);
        }
    }

    /// <summary>
    /// 按住／放開（「按住說話」那顆）：按下叫 <see cref="Down"/>，放開叫 <see cref="Up"/>。
    /// UGUI 的 `PointerUp` 會送回**按下的那個物件**，手指滑出去才放開也照樣收得到 ⇒ 不會卡在錄音中。
    /// （2026-10-29「app 一啟動就開麥克風」的根因是 `NextVoice` 有個叫 `Start()` 的方法被 Unity 當生命週期
    ///   自動呼叫，已改名 `BeginRecording`；跟這顆的指標事件無關，這裡不需要額外的「武裝」。）
    /// </summary>
    public sealed class NextHold : MonoBehaviour, IPointerDownHandler, IPointerUpHandler, IDragHandler
    {
        public Action Down;
        public Action Up;
        /// <summary>按著往上拖了多少（螢幕像素；往上為正）：「上滑取消」用（owner 2026-10-30）。</summary>
        public Action<float> Moved;
        private bool _held;

        public void OnPointerDown(PointerEventData eventData)
        {
            if (_held) return;
            _held = true;
            Down?.Invoke();
        }

        public void OnDrag(PointerEventData eventData)
        {
            if (!_held) return;
            Moved?.Invoke(eventData.position.y - eventData.pressPosition.y);
        }

        public void OnPointerUp(PointerEventData eventData)
        {
            if (!_held) return;
            _held = false;
            Up?.Invoke();
        }

        /// <summary>頁面被拆掉時還按著（例如換角色）⇒ 照放開處理，錄音不會一直開著。</summary>
        private void OnDisable()
        {
            if (!_held) return;
            _held = false;
            Up?.Invoke();
        }
    }

    /// <summary>
    /// 長按（owner 2026-10-30：訊息長按複製）。放在捲動區裡的東西上也行：不吃拖曳——
    /// 手指一動（拖動、超過一點距離）就當成在捲，不算長按，捲動照樣交給外面的 `ScrollRect`。
    /// </summary>
    public sealed class NextLongPress : MonoBehaviour, IPointerDownHandler, IPointerUpHandler, IPointerExitHandler
    {
        public Action Fire;
        public float Seconds = 0.5f;
        /// <summary>手指移動超過這麼多像素就不算長按。</summary>
        public float Slop = 24f;
        private PointerEventData _press;
        private Vector2 _start;
        private float _at;

        public void OnPointerDown(PointerEventData eventData)
        {
            _press = eventData;
            _start = eventData.position;
            _at = Time.unscaledTime;
        }

        public void OnPointerUp(PointerEventData eventData) { _press = null; }

        public void OnPointerExit(PointerEventData eventData) { _press = null; }

        private void Update()
        {
            if (_press == null) return;
            if (_press.dragging || (_press.position - _start).sqrMagnitude > Slop * Slop)
            {
                _press = null;
                return;
            }
            if (Time.unscaledTime - _at < Seconds) return;
            _press = null;
            Fire?.Invoke();
        }
    }

    /// <summary>一直轉（光環、讀取中）。</summary>
    public sealed class NextSpin : MonoBehaviour
    {
        public float DegreesPerSecond = 40f;

        private void Update()
        {
            transform.localRotation *= Quaternion.Euler(0f, 0f, -DegreesPerSecond * Time.unscaledDeltaTime);
        }
    }

    /// <summary>呼吸（連線中的小圓點）：縮放 1↔1.35、alpha 1↔0.5。</summary>
    public sealed class NextPulse : MonoBehaviour
    {
        public float Period = 1.4f;
        private Graphic _graphic;

        private void Awake() { _graphic = GetComponent<Graphic>(); }

        private void Update()
        {
            var wave = (Mathf.Sin(Time.unscaledTime * Mathf.PI * 2f / Period) + 1f) * 0.5f;
            var s = Mathf.Lerp(1f, 1.35f, wave);
            transform.localScale = new Vector3(s, s, 1f);
            if (_graphic == null) return;
            var color = _graphic.color;
            color.a = Mathf.Lerp(1f, 0.5f, wave);
            _graphic.color = color;
        }
    }

    /// <summary>
    /// 點擊特效：點到哪裡就從那裡擴一圈細環、飛出三片小三角（BA 的觸控回饋語言，原創畫法）。
    /// 掛在畫布根上；只用舊的 Input Manager（專案的 `activeInputHandler: 0`），觸控會映成滑鼠。
    /// </summary>
    public sealed class NextTapFx : MonoBehaviour
    {
        public Camera Camera;

        private void Update()
        {
            if (!Input.GetMouseButtonDown(0)) return;
            var canvas = (RectTransform)transform;
            if (!RectTransformUtility.ScreenPointToLocalPointInRectangle(canvas, Input.mousePosition, Camera, out var at))
                return;
            Burst(canvas, at);
        }

        public static void Burst(RectTransform canvas, Vector2 at)
        {
            var ring = Piece(canvas, "ring", at, 18f, NextTheme.Sky);
            NextMotion.Play(ring, 0.42f, p =>
            {
                var size = Mathf.Lerp(18f, 66f, p);
                ring.sizeDelta = new Vector2(size, size);
                SetAlpha(ring, 0.9f * (1f - p));
            }, 0f, NextMotion.OutCubic, () => Destroy(ring.gameObject));
            for (var i = 0; i < 3; i++)
            {
                var angle = (UnityEngine.Random.Range(0f, 120f) + i * 120f) * Mathf.Deg2Rad;
                var distance = UnityEngine.Random.Range(26f, 40f);
                var spin = UnityEngine.Random.Range(-220f, 220f);
                var tri = Piece(canvas, i == 1 ? "spark" : "tri", at, 9f, i == 1 ? NextTheme.Yellow : NextTheme.SkyTop);
                NextMotion.Play(tri, 0.48f, p =>
                {
                    tri.anchoredPosition = at + new Vector2(Mathf.Cos(angle), Mathf.Sin(angle)) * distance * p;
                    tri.localRotation = Quaternion.Euler(0f, 0f, spin * p);
                    var s = Mathf.Lerp(1f, 0.4f, p);
                    tri.localScale = new Vector3(s, s, 1f);
                    SetAlpha(tri, 1f - p);
                }, 0.02f * i, NextMotion.OutCubic, () => Destroy(tri.gameObject));
            }
        }

        private static RectTransform Piece(RectTransform canvas, string icon, Vector2 at, float size, Color tint)
        {
            var image = (RawImage)UiBuilder.Icon("TapFx", icon, canvas, size, tint);
            var rect = (RectTransform)image.transform;
            var element = rect.GetComponent<LayoutElement>() ?? rect.gameObject.AddComponent<LayoutElement>();
            element.ignoreLayout = true;
            rect.anchorMin = rect.anchorMax = new Vector2(0.5f, 0.5f);
            rect.anchoredPosition = at;
            rect.sizeDelta = new Vector2(size, size);
            rect.SetAsLastSibling();
            return rect;
        }

        private static void SetAlpha(RectTransform rect, float alpha)
        {
            var graphic = rect.GetComponent<Graphic>();
            if (graphic == null) return;
            var color = graphic.color;
            color.a = alpha;
            graphic.color = color;
        }
    }
}
