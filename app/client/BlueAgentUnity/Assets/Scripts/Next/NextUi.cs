using System;
using System.Collections.Generic;
using BlueAgent.Screens;
using TMPro;
using UnityEngine;
using UnityEngine.Events;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>
    /// Next 兩端共用的畫面元件（2026-10-29 改成 原創幾何風格的設計系統，顏色見 <see cref="NextTheme"/>）：
    /// 斜切按鈕、圖示鈕、頁首、卡片、徽章、頁籤、開關、分段選擇、清單列、頭像、輸入框、底圖。
    ///
    /// ⚠️ 與舊殼最大的差別：**這一版有真的 `EventSystem` ＋ `GraphicRaycaster`**（建場景時就掛），
    /// 所以按鈕用 UGUI 的 `Button`、長列表用 `ScrollRect`——不必再走 `ShellRouter` 那套自製命中／拖曳。
    /// 按鈕一律掛 <see cref="NextPress"/>（按下縮、放開彈），動態都在 <see cref="NextMotion"/>。
    /// </summary>
    public static class NextUi
    {
        // ── 舊名字（各頁還在用的語意色）：一律指向 NextTheme ─────────────────────
        public static readonly Color PageBg = NextTheme.PageTop;
        public static readonly Color CardBg = NextTheme.Panel;
        public static readonly Color CardLine = NextTheme.PanelLine;
        public static readonly Color Ink = NextTheme.Ink;
        public static readonly Color InkSoft = NextTheme.InkSoft;
        public static readonly Color Primary = NextTheme.Sky;
        public static readonly Color PrimaryInk = NextTheme.OnColor;
        public static readonly Color Danger = NextTheme.Danger;
        public static readonly Color Ok = NextTheme.Ok;
        public const float Radius = NextTheme.Radius;

        /// <summary>按鈕的樣式（不要在頁面裡自己挑顏色）。</summary>
        public enum Style
        {
            /// <summary>天藍漸層、白字：一般的確定。</summary>
            Primary,
            /// <summary>黃色、深藍字：這一頁最主要的那一個動作（送出、連線）。</summary>
            Accent,
            /// <summary>白底、深藍字：次要動作。</summary>
            Secondary,
            /// <summary>深海軍藍、白字：選中的頁籤、返回鍵。</summary>
            Navy,
            /// <summary>淡藍底、深藍字：未選中的頁籤／選項。</summary>
            Ghost,
            /// <summary>淡紅底、紅字：登出、撤銷。</summary>
            Danger,
        }

        private struct Paint
        {
            public Color Top;
            public Color Bottom;
            public Color Ink;
            public Color Line;
        }

        private static Paint PaintOf(Style style)
        {
            switch (style)
            {
                case Style.Accent:
                    return new Paint { Top = NextTheme.YellowTop, Bottom = NextTheme.Yellow, Ink = NextTheme.Navy };
                case Style.Secondary:
                    return new Paint { Top = Color.white, Bottom = Color.white, Ink = NextTheme.Navy, Line = NextTheme.PanelLine };
                case Style.Navy:
                    return new Paint { Top = NextTheme.NavyTop, Bottom = NextTheme.Navy, Ink = NextTheme.OnColor };
                case Style.Ghost:
                    return new Paint { Top = NextTheme.SkyPale, Bottom = NextTheme.SkyPale, Ink = NextTheme.Navy };
                case Style.Danger:
                    return new Paint { Top = NextTheme.DangerPale, Bottom = NextTheme.DangerPale, Ink = NextTheme.Danger };
                default:
                    return new Paint { Top = NextTheme.SkyTop, Bottom = NextTheme.Sky, Ink = NextTheme.OnColor };
            }
        }

        // ── 畫布與底圖 ─────────────────────────────────────────────────────────

        /// <summary>
        /// 建主畫布（**整個版面就是這一張**：topbar 固定高、body 吃剩下的高度）。
        /// 與 `AppShell` 一樣走 `UiBuilder.NewCanvas`（設計格 460×818，寬度貼合視野）。
        /// </summary>
        public static Canvas NewPage(Camera camera, string name)
        {
            UiBuilder.SelectTextBackend();
            NextFonts.ApplyUi();                  // 界面字跟界面語言換字型（Noto Sans JP／SC／TC）
            UiBuilder.FillViewport = true;
            UiBuilder.DesignWidth = NextLayout.DesignWidth();   // 大螢幕（Duo 展開屏）1 單位≈1 pt
            camera.clearFlags = CameraClearFlags.SolidColor;
            camera.backgroundColor = NextTheme.PageBottom;
            var canvas = UiBuilder.NewCanvas(camera, name);
            if (canvas.GetComponent<GraphicRaycaster>() == null) canvas.gameObject.AddComponent<GraphicRaycaster>();
            var fx = canvas.gameObject.AddComponent<NextTapFx>();
            fx.Camera = camera;
            return canvas;
        }

        /// <summary>
        /// 底圖：上淡下深的天藍漸層＋斜紋＋角落兩片大三角與一圈慢慢轉的光環（都很淡，只是氛圍）。
        /// 鋪在畫布最底層、不吃點擊、不佔版面。
        /// </summary>
        public static void Background(RectTransform canvas)
        {
            var host = UiBuilder.Overlay("NextBackdrop", canvas);
            host.SetAsFirstSibling();
            var fill = UiBuilder.Overlay("Gradient", host);
            var image = fill.gameObject.AddComponent<Image>();
            image.raycastTarget = false;
            var gradient = fill.gameObject.AddComponent<NextGradient>();
            gradient.Top = NextTheme.PageTop;
            gradient.Bottom = NextTheme.PageBottom;

            var stripes = UiBuilder.Overlay("Stripes", host);
            var raw = stripes.gameObject.AddComponent<RawImage>();
            raw.texture = StripeTexture();
            raw.color = NextTheme.Pattern;
            raw.raycastTarget = false;
            var size = canvas.rect.size;
            raw.uvRect = new Rect(0f, 0f, Mathf.Max(1f, size.x / 96f), Mathf.Max(1f, size.y / 96f));

            Decor(host, "tri", new Vector2(0f, 0f), new Vector2(70f, 60f), 230f, 18f, new Color(1f, 1f, 1f, 0.45f));
            var halo = Decor(host, "halo", new Vector2(1f, 1f), new Vector2(-60f, -110f), 260f, 0f,
                             new Color(NextTheme.Sky.r, NextTheme.Sky.g, NextTheme.Sky.b, 0.08f));
            halo.gameObject.AddComponent<NextSpin>().DegreesPerSecond = 5f;
        }

        private static RectTransform Decor(RectTransform host, string icon, Vector2 anchor, Vector2 at, float size,
                                           float degrees, Color tint)
        {
            var rect = (RectTransform)UiBuilder.Icon("Decor_" + icon, icon, host, size, tint).transform;
            var element = rect.GetComponent<LayoutElement>() ?? rect.gameObject.AddComponent<LayoutElement>();
            element.ignoreLayout = true;
            rect.anchorMin = rect.anchorMax = anchor;
            rect.anchoredPosition = at;
            rect.sizeDelta = new Vector2(size, size);
            rect.localRotation = Quaternion.Euler(0f, 0f, degrees);
            return rect;
        }

        private static Texture2D _stripes;

        /// <summary>斜紋：96×96、45° 的寬淡條（RawImage 鋪滿重複）。執行期畫一次，所有頁共用。</summary>
        private static Texture2D StripeTexture()
        {
            if (_stripes != null) return _stripes;
            const int size = 96;
            var texture = new Texture2D(size, size, TextureFormat.RGBA32, false) { wrapMode = TextureWrapMode.Repeat };
            var pixels = new Color32[size * size];
            for (var y = 0; y < size; y++)
            {
                for (var x = 0; x < size; x++)
                {
                    var band = (x + y) % 48;
                    var alpha = band < 14 ? (byte)Mathf.RoundToInt(255f * Mathf.Sin(Mathf.PI * band / 14f)) : (byte)0;
                    pixels[y * size + x] = new Color32(255, 255, 255, alpha);
                }
            }
            texture.SetPixels32(pixels);
            texture.Apply();
            _stripes = texture;
            return texture;
        }

        // ── 按鈕 ───────────────────────────────────────────────────────────────

        /// <summary>
        /// 一顆按鈕：斜切的底板（漸層＋落影）＋可選的圖示＋字。`width` 0 ⇒ 照內容寬（呼叫端可再給 Weight）。
        /// </summary>
        public static Button Button(string name, RectTransform parent, string label, Style style,
                                    float height = 38f, float width = 0f, string icon = null, bool skew = true,
                                    float fontSize = NextTheme.LabelSize)
        {
            var paint = PaintOf(style);
            var pad = 14 + (skew ? Mathf.RoundToInt(height * NextTheme.Skew * 0.5f) : 0);
            var rect = UiBuilder.Row(name, parent, 6f, new RectOffset(pad, pad, 0, 0), TextAnchor.MiddleCenter, false);
            UiBuilder.FixedHeight(rect, height);
            if (width > 0f) UiBuilder.FixedWidth(rect, width);

            var plate = Plate(name + "Bg", rect, paint, skew ? NextTheme.Skew : 0f, NextTheme.SmallRadius, true);
            if (!string.IsNullOrEmpty(icon))
                UiBuilder.Icon(name + "Icon", icon, rect, Mathf.Round(height * 0.46f), paint.Ink);
            if (!string.IsNullOrEmpty(label))
                UiBuilder.Text(name + "Text", rect, label, fontSize, paint.Ink, TextAlignmentOptions.Center,
                               noWrap: true, bold: true);

            var button = rect.gameObject.AddComponent<Button>();
            button.targetGraphic = plate;
            button.transition = Selectable.Transition.None;
            rect.gameObject.AddComponent<NextPress>();
            return button;
        }

        /// <summary>只有圖示的方鈕／圓鈕（返回、設定、+、麥克風）。</summary>
        public static Button IconButton(string name, RectTransform parent, string icon, Style style,
                                        float size = 38f, bool round = false)
        {
            var paint = PaintOf(style);
            var rect = UiBuilder.Row(name, parent, 0f, null, TextAnchor.MiddleCenter, false);
            UiBuilder.FixedHeight(rect, size);
            UiBuilder.FixedWidth(rect, size);
            var plate = Plate(name + "Bg", rect, paint, 0f, round ? size / 2f : NextTheme.Radius, true);
            UiBuilder.Icon(name + "Icon", icon, rect, Mathf.Round(size * 0.52f), paint.Ink);
            var button = rect.gameObject.AddComponent<Button>();
            button.targetGraphic = plate;
            button.transition = Selectable.Transition.None;
            rect.gameObject.AddComponent<NextPress>();
            return button;
        }

        /// <summary>按鈕的底板：圓角圖＋（可選）斜切＋漸層＋細框＋落影。</summary>
        private static Image Plate(string name, RectTransform parent, Paint paint, float skew, float radius, bool shadow)
        {
            var bg = UiBuilder.Overlay(name, parent);
            bg.SetAsFirstSibling();
            var image = bg.gameObject.AddComponent<Image>();
            UiBuilder.Round(image, radius);
            image.raycastTarget = true;
            if (skew > 0f) bg.gameObject.AddComponent<NextSkew>().Amount = skew;
            var gradient = bg.gameObject.AddComponent<NextGradient>();
            gradient.Top = paint.Top;
            gradient.Bottom = paint.Bottom;
            if (paint.Line.a > 0f)
            {
                var outline = bg.gameObject.AddComponent<Outline>();
                outline.effectColor = paint.Line;
                outline.effectDistance = new Vector2(1f, -1f);
            }
            if (shadow)
            {
                var drop = bg.gameObject.AddComponent<Shadow>();
                drop.effectColor = NextTheme.Shadow;
                drop.effectDistance = new Vector2(0f, -2.5f);
            }
            return image;
        }

        /// <summary>按鈕裡那段字（換字、換顏色用）。</summary>
        public static TMP_Text LabelOf(Button button)
        {
            return button != null ? button.GetComponentInChildren<TMP_Text>(true) : null;
        }

        // ── 頁首／段落／卡片 ──────────────────────────────────────────────────────

        /// <summary>
        /// 每一頁最上面那一列：深藍方形返回鍵＋天藍斜條＋粗斜體標題。回傳右側的空位（要放東西就往裡加）。
        /// </summary>
        public static RectTransform Header(string name, RectTransform parent, string title, UnityAction back)
        {
            var row = UiBuilder.Row(name, parent, 8f, new RectOffset(0, 0, 0, 0), TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(row, 36f);
            if (back != null)
            {
                var button = IconButton(name + "Back", row, "chevron-left", Style.Navy, 38f);
                button.onClick.AddListener(back);
            }
            Accent(name + "Accent", row, 5f, 20f);
            var text = UiBuilder.Text(name + "Title", row, title, NextTheme.TitleSize, NextTheme.Ink,
                                      TextAlignmentOptions.MidlineLeft, weight: 1f, noWrap: true, bold: true);
            Italic(text);
            var slot = UiBuilder.Row(name + "Slot", row, 6f, null, TextAnchor.MiddleRight, false);
            return slot;
        }

        /// <summary>天藍的小斜條（標題前的記號）。</summary>
        public static void Accent(string name, RectTransform parent, float width, float height)
        {
            var bar = UiBuilder.NewRect(name, parent);
            UiBuilder.FixedWidth(bar, width);
            UiBuilder.FixedHeight(bar, height);
            var image = bar.gameObject.AddComponent<Image>();
            image.color = NextTheme.Sky;
            image.raycastTarget = false;
            bar.gameObject.AddComponent<NextSkew>().Amount = 0.3f;
        }

        public static void Italic(Component text)
        {
            if (text is TMP_Text tmp) tmp.fontStyle |= FontStyles.Italic;
        }

        /// <summary>卡片：白底、細框、圓角、淡落影，右上角一片小三角。內容照 `Column` 排。</summary>
        public static RectTransform Card(string name, Transform parent, float pad = 12f, float gap = 7f)
        {
            var card = UiBuilder.Column(name, parent, gap, new RectOffset((int)pad, (int)pad, (int)pad, (int)pad));
            var bg = UiBuilder.Backdrop(name + "Bg", card, NextTheme.Panel, radius: NextTheme.Radius + 2f);
            var drop = bg.gameObject.AddComponent<Shadow>();
            drop.effectColor = NextTheme.Shadow;
            drop.effectDistance = new Vector2(0f, -3f);
            UiBuilder.Border(card, NextTheme.PanelLine, 1f, NextTheme.Radius + 2f);
            // 左上角一小段天藍斜條（BA 面板常見的「角標」，不佔版面）
            var tab = UiBuilder.NewRect(name + "Tab", card);
            tab.gameObject.AddComponent<LayoutElement>().ignoreLayout = true;
            tab.anchorMin = tab.anchorMax = new Vector2(0f, 1f);
            tab.pivot = new Vector2(0f, 1f);
            tab.anchoredPosition = new Vector2(14f, 0f);
            tab.sizeDelta = new Vector2(34f, 3f);
            var tabImage = tab.gameObject.AddComponent<Image>();
            tabImage.color = NextTheme.Sky;
            tabImage.raycastTarget = false;
            tab.gameObject.AddComponent<NextSkew>().Amount = 1f;
            return card;
        }

        /// <summary>卡片裡的段落標題：小斜條＋粗字（可帶圖示）。</summary>
        public static Component Section(string name, RectTransform parent, string text, string icon = null)
        {
            var row = UiBuilder.Row(name + "Row", parent, 6f, null, TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(row, 20f);
            if (!string.IsNullOrEmpty(icon)) UiBuilder.Icon(name + "Icon", icon, row, 16f, NextTheme.Sky);
            else Accent(name + "Accent", row, 4f, 14f);
            return UiBuilder.Text(name, row, text, NextTheme.HeadSize, NextTheme.Ink,
                                  TextAlignmentOptions.MidlineLeft, weight: 1f, noWrap: true, bold: true);
        }

        /// <summary>說明文字（小字、灰）。</summary>
        public static Component Note(string name, RectTransform parent, string text, Color? color = null)
        {
            return UiBuilder.Text(name, parent, text, NextTheme.NoteSize, color ?? NextTheme.InkSoft, TextAlignmentOptions.TopLeft);
        }

        /// <summary>細分隔線。</summary>
        public static void Divider(string name, RectTransform parent)
        {
            var line = UiBuilder.NewRect(name, parent);
            UiBuilder.FixedHeight(line, 1f);
            var image = line.gameObject.AddComponent<Image>();
            image.color = NextTheme.Divider;
            image.raycastTarget = false;
        }

        /// <summary>一行「標籤：值」（帳號列、狀態頁共用）：標籤固定寬、灰；值吃剩下、深色。</summary>
        public static Component KeyValue(string name, RectTransform parent, string key, string value,
                                         float size = 11.5f, bool wrap = true)
        {
            var row = UiBuilder.Row(name, parent, 8f, new RectOffset(0, 0, 2, 2), TextAnchor.UpperLeft, false);
            var label = UiBuilder.Text(name + "Key", row, key, size, NextTheme.InkSoft, TextAlignmentOptions.TopLeft, noWrap: true);
            UiBuilder.FixedWidth((RectTransform)label.transform, 104f);
            var shown = UiBuilder.Text(name + "Value", row, value, size, NextTheme.Ink, TextAlignmentOptions.TopLeft,
                                       weight: 1f, noWrap: !wrap);
            // ⚠️ `weight` 會連縱向也設成可伸展 ⇒ 卡片被撐高時（設定的サービス／診斷），多出來的高度平均分給
            // 每一行，標籤和值之間空出一大塊（2026-10-30 全頁截圖抓到）。值只該橫向吃剩下的寬。
            var element = shown.GetComponent<LayoutElement>();
            if (element != null) element.flexibleHeight = 0f;
            return shown;
        }

        // ── 徽章／頁籤／開關／分段 ────────────────────────────────────────────────

        /// <summary>斜切的小徽章（身份、連線狀態、未讀）。`dot` 給顏色就在字前面加一顆圓點。</summary>
        public static RectTransform Badge(string name, RectTransform parent, string text, Color bg, Color ink,
                                          Color? dot = null)
        {
            var rect = UiBuilder.Row(name, parent, 5f, new RectOffset(10, 10, 2, 2), TextAnchor.MiddleCenter, false);
            var plate = UiBuilder.Backdrop(name + "Bg", rect, bg, radius: 3f);
            plate.gameObject.AddComponent<NextSkew>().Amount = NextTheme.Skew;
            if (dot.HasValue) UiBuilder.Icon(name + "Dot", "dot-fill", rect, 8f, dot.Value);
            UiBuilder.Text(name + "Text", rect, text, 10.5f, ink, TextAlignmentOptions.Center, noWrap: true, bold: true);
            var layout = rect.gameObject.AddComponent<LayoutElement>();
            layout.minHeight = 20f;
            return rect;
        }

        /// <summary>頁籤列：每一格一顆斜切鈕（選中＝深藍），平均分寬。</summary>
        public static void Tabs(string name, RectTransform parent, IList<(string key, string label, string icon)> items,
                                string selected, Action<string> pick)
        {
            var row = UiBuilder.Row(name, parent, 4f, new RectOffset(4, 4, 0, 0), TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(row, 32f);
            foreach (var (key, label, icon) in items)
            {
                var on = key == selected;
                var tab = Button(name + "_" + key, row, label, on ? Style.Navy : Style.Secondary, 30f, 0f, icon,
                                 true, 11.5f);
                UiBuilder.EqualWidth((RectTransform)tab.transform);
                FitTabLabel(tab, name + "_" + key + "Text");
                var target = key;
                tab.onClick.AddListener(() => pick(target));
            }
        }

        /// <summary>
        /// 頁籤按鈕窄（設定五頁等分，手機上一格約 70pt）：一般按鈕兩側各 ~17pt 內距＋圖示＋原尺寸字
        /// 放不下「サービス」，字從置中往兩邊溢出、壓到左邊的圖示（2026-11-17 模擬器實測）。
        /// ⇒ 頁籤自己收窄內距，字只拿剩下的寬度，放不下就縮字（11.5 → 最小 8.5）；其他按鈕不受影響。
        /// </summary>
        private static void FitTabLabel(Button tab, string textName)
        {
            var group = tab.GetComponent<HorizontalLayoutGroup>();
            if (group != null)
            {
                var pad = 6 + Mathf.RoundToInt(30f * NextTheme.Skew * 0.5f);
                group.padding = new RectOffset(pad, pad, 0, 0);
                group.spacing = 3f;
            }
            var text = tab.transform.Find(textName);
            if (text == null) return;
            UiBuilder.EqualWidth((RectTransform)text);
            var tmp = text.GetComponent<TMP_Text>();
            if (tmp != null)
            {
                tmp.enableAutoSizing = true;
                tmp.fontSizeMin = 8.5f;
                tmp.fontSizeMax = tmp.fontSize;
                return;
            }
            var legacy = text.GetComponent<Text>();
            if (legacy != null)
            {
                legacy.resizeTextForBestFit = true;
                legacy.resizeTextMinSize = 8;
                legacy.resizeTextMaxSize = legacy.fontSize;
            }
        }

        /// <summary>
        /// 開關（膠囊軌道＋白色圓鈕）：點一下鈕先滑過去（0.18 秒），滑完才叫 <paramref name="set"/>
        /// （呼叫端通常會重畫整頁，先叫的話動畫還沒播就被拆掉了）。
        /// </summary>
        public static Button Toggle(string name, RectTransform parent, bool on, Action<bool> set)
        {
            var rect = UiBuilder.NewRect(name, parent);
            UiBuilder.FixedWidth(rect, 50f);
            UiBuilder.FixedHeight(rect, 28f);
            var track = rect.gameObject.AddComponent<Image>();
            UiBuilder.Round(track, 14f);
            track.color = on ? NextTheme.Sky : NextTheme.Idle;
            var knob = UiBuilder.NewRect(name + "Knob", rect);
            knob.anchorMin = knob.anchorMax = new Vector2(0f, 0.5f);
            knob.sizeDelta = new Vector2(22f, 22f);
            knob.anchoredPosition = new Vector2(on ? 36f : 14f, 0f);
            var knobImage = knob.gameObject.AddComponent<Image>();
            UiBuilder.Round(knobImage, 11f);
            knobImage.raycastTarget = false;
            knob.gameObject.AddComponent<Shadow>().effectColor = NextTheme.Shadow;
            var button = rect.gameObject.AddComponent<Button>();
            button.targetGraphic = track;
            button.transition = Selectable.Transition.None;
            button.onClick.AddListener(() =>
            {
                var next = !on;
                NextMotion.Play(knob, 0.18f, p =>
                {
                    knob.anchoredPosition = new Vector2(Mathf.Lerp(on ? 36f : 14f, next ? 36f : 14f, p), 0f);
                    track.color = Color.Lerp(on ? NextTheme.Sky : NextTheme.Idle, next ? NextTheme.Sky : NextTheme.Idle, p);
                }, 0f, NextMotion.OutCubic, () => set(next));
            });
            return button;
        }

        /// <summary>分段選擇（語言、背景…）：一排斜切鈕，選中那格深藍。</summary>
        public static void Segments(string name, RectTransform parent, IList<string> labels, int selected, Action<int> pick,
                                    float height = 30f)
        {
            var row = UiBuilder.Row(name, parent, 4f, new RectOffset(4, 4, 0, 0), TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(row, height);
            for (var i = 0; i < labels.Count; i++)
            {
                var index = i;
                var cell = Button(name + "_" + i, row, labels[i], i == selected ? Style.Navy : Style.Ghost,
                                  height - 2f, 0f, i == selected ? "check" : null, true, 11.5f);
                UiBuilder.EqualWidth((RectTransform)cell.transform);
                cell.onClick.AddListener(() => pick(index));
            }
        }

        /// <summary>音量那種「格數」：一排斜切小格，亮到第 <paramref name="level"/> 格；點哪格就設到哪格。</summary>
        public static void Meter(string name, RectTransform parent, int level, int max, Action<int> set)
        {
            var row = UiBuilder.Row(name, parent, 4f, new RectOffset(4, 4, 0, 0), TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(row, 24f);
            for (var i = 1; i <= max; i++)
            {
                var cell = UiBuilder.NewRect(name + "_" + i, row);
                UiBuilder.FixedHeight(cell, 8f + 12f * i / max);
                UiBuilder.EqualWidth(cell);
                var image = cell.gameObject.AddComponent<Image>();
                image.color = i <= level ? NextTheme.Sky : NextTheme.IdlePale;
                cell.gameObject.AddComponent<NextSkew>().Amount = 0.3f;
                var button = cell.gameObject.AddComponent<Button>();
                button.targetGraphic = image;
                button.transition = Selectable.Transition.None;
                var value = i;
                button.onClick.AddListener(() => set(value));
            }
        }

        /// <summary>清單列（設定裡的「帳號管理」「登出」）：圖示＋字＋右箭頭，整列可點。</summary>
        public static Button ListRow(string name, RectTransform parent, string icon, string label, UnityAction click,
                                     bool danger = false)
        {
            var row = UiBuilder.Row(name, parent, 10f, new RectOffset(4, 4, 0, 0), TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(row, 40f);
            var hit = row.gameObject.AddComponent<Image>();
            hit.color = new Color(1f, 1f, 1f, 0.001f);
            var ink = danger ? NextTheme.Danger : NextTheme.Ink;
            UiBuilder.Icon(name + "Icon", icon, row, 18f, danger ? NextTheme.Danger : NextTheme.Sky);
            UiBuilder.Text(name + "Text", row, label, NextTheme.BodySize, ink, TextAlignmentOptions.MidlineLeft,
                           weight: 1f, noWrap: true, bold: true);
            UiBuilder.Icon(name + "Go", "chevron-right", row, 14f, NextTheme.InkFaint);
            var button = row.gameObject.AddComponent<Button>();
            button.targetGraphic = hit;
            button.transition = Selectable.Transition.None;
            button.onClick.AddListener(click);
            row.gameObject.AddComponent<NextPress>();
            return button;
        }

        /// <summary>
        /// 底部彈起選單（聊天頁「+」，QQ 那種）裡的一格：圓角方塊圖示＋下面一行字；整格可點。
        /// 平均分寬（`EqualWidth`）。<paramref name="accent"/> 那一格用黃色方塊（最常用的入口：送照片）。
        /// </summary>
        public static Button SheetItem(string name, RectTransform parent, string icon, string label, bool accent = false)
        {
            var item = UiBuilder.Column(name, parent, 6f, new RectOffset(0, 0, 2, 2), true, TextAnchor.UpperCenter);
            UiBuilder.EqualWidth(item);
            var hit = item.gameObject.AddComponent<Image>();
            hit.color = new Color(1f, 1f, 1f, 0.001f);              // 透明但吃點擊：整格都是命中區
            var badge = UiBuilder.Row(name + "Badge", item, 0f, null, TextAnchor.MiddleCenter);
            UiBuilder.FixedHeight(badge, 54f);
            var square = UiBuilder.NewRect(name + "Square", badge);
            square.gameObject.AddComponent<LayoutElement>().ignoreLayout = true;
            square.anchorMin = square.anchorMax = new Vector2(0.5f, 0.5f);
            square.sizeDelta = new Vector2(54f, 54f);
            var face = square.gameObject.AddComponent<Image>();
            UiBuilder.Round(face, 16f);
            face.color = accent ? NextTheme.Yellow : NextTheme.SkyPale;     // 選單底是白的 ⇒ 方塊用淡天藍才分得出來
            face.raycastTarget = false;
            var drop = square.gameObject.AddComponent<Shadow>();
            drop.effectColor = NextTheme.Shadow;
            drop.effectDistance = new Vector2(0f, -2.5f);
            UiBuilder.Icon(name + "Icon", icon, badge, 26f, accent ? NextTheme.Navy : NextTheme.Sky);
            UiBuilder.Text(name + "Text", item, label, 10.5f, NextTheme.Ink, TextAlignmentOptions.Top, noWrap: true, bold: true);
            var button = item.gameObject.AddComponent<Button>();
            button.targetGraphic = hit;
            button.transition = Selectable.Transition.None;
            item.gameObject.AddComponent<NextPress>();
            return button;
        }

        // ── 頭像／輸入框 ──────────────────────────────────────────────────────────

        /// <summary>圓形頭像（`Resources/avatars/{id}.png`，沒有就用 `_fallback`）＋外圈。</summary>
        public static RectTransform Avatar(string name, RectTransform parent, string entityId, float size, Color ring)
        {
            var rect = UiBuilder.NewRect(name, parent);
            UiBuilder.FixedWidth(rect, size);
            UiBuilder.FixedHeight(rect, size);
            var mask = rect.gameObject.AddComponent<Image>();
            UiBuilder.Round(mask, size / 2f);
            mask.color = Color.white;
            mask.raycastTarget = false;
            rect.gameObject.AddComponent<Mask>().showMaskGraphic = true;
            var face = UiBuilder.Overlay(name + "Face", rect);
            var image = face.gameObject.AddComponent<Image>();
            image.raycastTarget = false;
            image.preserveAspect = true;
            var texture = Resources.Load<Texture2D>("avatars/" + entityId) ?? Resources.Load<Texture2D>("avatars/_fallback");
            SetOwnedSprite(image, texture, ownTexture: false);
            var border = UiBuilder.Overlay(name + "Ring", rect);
            UiBuilder.Border(border, ring, 2f, size / 2f);
            return rect;
        }

        /// <summary>輸入框：淡藍底＋細框的圓角欄（字與提示字照界面字型）。</summary>
        public static TMP_InputField Field(string name, RectTransform parent, string placeholder, float height = 40f,
                                           float radius = NextTheme.Radius)
        {
            var field = UiBuilder.Input(name, parent, placeholder, NextTheme.BodySize, NextTheme.Ink, NextTheme.InkFaint,
                                        height, radius, 12f);
            var bg = field.GetComponent<Image>();
            if (bg != null) bg.color = NextTheme.Field;
            UiBuilder.Border((RectTransform)field.transform, NextTheme.PanelLine, 1f, radius);
            return field;
        }

        // ── 捲動／文字工具 ────────────────────────────────────────────────────────

        /// <summary>
        /// 可捲動欄（三層：host 吃版面 → viewport 遮罩 → content 自己長高）。
        /// 回傳 content（往裡面加東西）；`ScrollRect` 會自動貼底（`stickToBottom`）。
        /// </summary>
        public static RectTransform ScrollArea(string name, RectTransform parent, float spacing,
                                               RectOffset pad, out ScrollRect scroll)
        {
            var host = UiBuilder.NewRect(name + "Host", parent);
            UiBuilder.Weight(host, 1f);

            var viewport = UiBuilder.NewRect(name + "Viewport", host);
            viewport.gameObject.AddComponent<LayoutElement>().ignoreLayout = true;
            UiBuilder.Stretch(viewport);
            var mask = viewport.gameObject.AddComponent<RectMask2D>();
            mask.padding = Vector4.zero;
            viewport.gameObject.AddComponent<Image>().color = new Color(1f, 1f, 1f, 0f);

            var content = UiBuilder.Column(name, viewport, spacing, pad, true, TextAnchor.UpperLeft);
            var fitter = content.gameObject.AddComponent<ContentSizeFitter>();
            fitter.verticalFit = ContentSizeFitter.FitMode.PreferredSize;
            content.anchorMin = new Vector2(0f, 1f);
            content.anchorMax = new Vector2(1f, 1f);
            content.pivot = new Vector2(0.5f, 1f);
            content.anchoredPosition = Vector2.zero;
            content.sizeDelta = new Vector2(0f, content.sizeDelta.y);

            scroll = host.gameObject.AddComponent<ScrollRect>();
            scroll.content = content;
            scroll.viewport = viewport;
            scroll.horizontal = false;
            scroll.vertical = true;
            scroll.movementType = ScrollRect.MovementType.Clamped;
            scroll.scrollSensitivity = 18f;
            scroll.inertia = true;
            scroll.decelerationRate = 0.12f;
            return content;
        }

        /// <summary>把可捲動欄捲到最底（新訊息進來時叫）。</summary>
        public static void StickToBottom(ScrollRect scroll)
        {
            if (scroll == null) return;
            Canvas.ForceUpdateCanvases();
            LayoutRebuilder.ForceRebuildLayoutImmediate(scroll.content);
            scroll.verticalNormalizedPosition = 0f;
        }

        /// <summary>
        /// 改一段文字的內容——**TMP 與 legacy `Text` 都吃**。
        /// 為什麼要有這支：`UiBuilder.Text` 回的是 `Component`（後端由 `SelectTextBackend` 決定），
        /// 直接 `as TMP_Text` 在 legacy 後端會變 null（畫面靜靜地不更新）。
        /// </summary>
        public static void SetText(Component target, string text)
        {
            if (target == null) return;
            var tmp = target as TMP_Text;
            if (tmp != null) { tmp.text = text ?? ""; return; }
            var legacy = target as UnityEngine.UI.Text;
            if (legacy != null) legacy.text = text ?? "";
        }

        public static void SetColor(Component target, Color color)
        {
            if (target == null) return;
            var tmp = target as TMP_Text;
            if (tmp != null) { tmp.color = color; return; }
            var legacy = target as UnityEngine.UI.Text;
            if (legacy != null) legacy.color = color;
        }

        /// <summary>
        /// 把貼圖掛到 `image` 上，Sprite（與 <paramref name="ownTexture"/> 時的貼圖）歸那顆物件所有
        /// （`NextOwnedSprite`：物件銷毀／換圖就釋放，U-7）。**Next 裡唯一准 `Sprite.Create` 的地方。**
        ///
        /// 非同步回來時 `image` 可能已經被銷毀（頁面早就換掉）⇒ 不寫它，自己的貼圖就地釋放，回 false。
        /// `Resources` 載入的共用貼圖要傳 `ownTexture: false`（銷毀資產會報錯，也會害到別人）。
        /// </summary>
        public static bool SetOwnedSprite(Image image, Texture2D texture, bool ownTexture = true)
        {
            if (texture == null) return false;
            if (image == null)
            {
                if (ownTexture) UnityEngine.Object.Destroy(texture);
                return false;
            }
            var owner = image.GetComponent<NextOwnedSprite>();
            if (owner == null) owner = image.gameObject.AddComponent<NextOwnedSprite>();
            var sprite = Sprite.Create(texture, new Rect(0f, 0f, texture.width, texture.height),
                                       new Vector2(0.5f, 0.5f));
            owner.Replace(sprite, ownTexture ? texture : null);
            image.sprite = sprite;
            return true;
        }
    }
}
