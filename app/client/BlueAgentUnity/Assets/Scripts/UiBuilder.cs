using System;
using System.Collections.Generic;
using System.Text.RegularExpressions;
using TMPro;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.UI;

namespace BlueAgent.Screens
{
    /// <summary>
    /// **最小的宣告式 UGUI 建立器**——M3 的每一屏都只用這幾支組出來。
    ///
    /// 為什麼要有它（而不是每屏自己 `new GameObject`）：真相那一側是
    /// **Android 的 `LinearLayout` weight**（作者在 `NativeHome.kt:167` 自己寫「weight ＝ CSS 的 fr」）
    /// 與 **webui 的 flexbox**；UGUI 的對應物是 `LayoutGroup` ＋ `LayoutElement.flexible???`
    /// ⇒ 把「垂直／水平容器＋每格的 weight」收成兩支函式，**版面數值就能照抄真相**，
    /// 不必用座標硬拉（硬拉＝每屏都要人眼調，也就沒法機械對拍）。
    ///
    /// ⚠️ 這一層**不做任何業務判斷**（誰可以按、字要寫什麼、要不要顯示）——那些都在共用邏輯層的投影裡
    /// （`HomeState.From` 等，已 fixture 對拍綠）。這裡只負責「把投影畫出來」。
    /// </summary>
    public static class UiBuilder
    {
        /// <summary>
        /// 畫布基準尺寸＝**真相的檢視框**（`DesignTokens.Reference`，460×818）——數值的單一來源在那裡。
        /// </summary>
        public static Vector2 Reference => DesignTokens.Reference;

        /// <summary>相機距離（畫布放在相機前面這麼遠；正交相機下不影響大小，只影響前後遮擋）。</summary>
        private const float PlaneDistance = 5f;

        /// <summary>
        /// UI 圖層＝Unity 內建的 `UI`（第 5 層）。
        ///
        /// 為什麼要用圖層分開：M3 的合成是**兩台相機**——「模型相機」把立繪渲進 RenderTexture
        /// （畫進舞台那一格的 `RawImage`），「UI 相機」只畫 UI。要靠 cullingMask 把兩邊分乾淨，
        /// 而用內建的 UI 層就不必去改 `TagManager.asset`（新開一層要改專案設定，多一個檔案要同步）。
        /// </summary>
        public const int UiLayer = 5;

        /// <summary>這一屏的相機（＝截圖要拍的那一台；由 <see cref="NewCanvas"/> 記下來）。</summary>
        public static Camera LastCanvasCamera { get; private set; }

        /// <summary>
        /// 建一個畫布：**WorldSpace ＋固定尺寸（<see cref="Reference"/>）＋掛在相機前面**。
        ///
        /// 為什麼不是 ScreenSpaceOverlay／ScreenSpaceCamera（踩過才知道的）：
        /// * Overlay 在 `-batchmode` 沒有螢幕後緩衝區 ⇒ `SubmitRenderRequest`（相機渲進紋理）
        ///   那條**已驗證過的通道**根本拍不到它；
        /// * ScreenSpaceCamera 的畫布尺寸是**相機視口對應的螢幕像素**，而無頭下的「螢幕尺寸」
        ///   是假的（實測 1687×…）⇒ 版面會照那個尺寸排、再被塞進 720×1280 的目標圖，**歪掉且不可控**。
        ///
        /// **合成＝「UI 在前、舞台那一格留一個洞」**（一台相機、一次渲請求就拍完）：
        /// 畫布掛在相機正前方（比模型更靠近相機）⇒ UI 蓋在立繪上；畫布裡除舞台那一格外都有底色
        /// ⇒ 立繪只從那一格透出來。
        ///
        /// **縮放**：畫布的 1280 設計單位縮到剛好蓋滿相機的正交視高（`orthographicSize * 2`，
        /// 那是立繪那一側的自然取景）⇒ 設計座標與畫面 1:1；寬度方向自動對上（aspect 已按 720:1280 設好）。
        ///
        /// 為什麼不是「兩台相機＋RenderTexture」（試過，走不通）：**一次運行只拍得成一張**——
        /// 實測第二次 `SubmitRenderRequest` 只拿得到底色（換新的目標紋理也一樣，見
        /// `HeadlessBuild.CaptureTo`）。立繪要進 RenderTexture 就得再渲一次，那就沒圖可以拍了。
        /// </summary>
        /// <summary>
        /// **畫布填滿視野**（app 的殼那一條；owner 2026-10-12「手機屏幕不是 16:9」＋
        /// 「UI 全擠在頂部、下面留太大空白」）。
        ///
        /// 設 true 時 `NewCanvas` 改成「**按寬度貼合**（手機比 9:16 窄，按高度貼合會左右裁）＋
        /// **畫布高度＝視野高**」：多出來的高度就交給**彈性那一列**（`minmax(180px,1fr)` 的舞台）——
        /// 這與瀏覽器在更高的視窗裡排版**同一個語意**（`.app-shell` 是 `min-height:100%`，flex 區塊自己長）。
        ///
        /// ⚠️ **必須在「建屏之前」就定好**：各屏的 grid 列高是 `PinGridRows` 照 body 的
        /// `rect.height` 當場算出來再釘死的 ⇒ 建完屏才把畫布拉高，列高還是舊的（舞台 384），
        /// 多出來的高度沒人吃 ⇒ 症狀＝**整片擠在頂部、下面一大塊空白**（2026-10-12 真機）。
        ///
        /// ⚠️ 離線出圖那條路（`render_screen.sh`／逐像素對拍）**不設**⇒ 維持設計格 460×818 不變。
        /// </summary>
        public static bool FillViewport;

        /// <summary>
        /// 填滿視野時畫布要幾個設計單位寬（0＝照舊用 <see cref="Reference"/> 的 460）。
        /// Next 在大螢幕（iPhone Duo 展開屏）設成螢幕的 pt 寬，免得把 460 放大成一條（owner 2026-10-30）。
        /// </summary>
        public static float DesignWidth;

        /// <summary>上一次 `NewCanvas` 實際用的畫布尺寸（殼的 log 要對「填滿」有沒有生效）。</summary>
        public static Vector2 LastCanvasSize;

        /// <summary>
        /// **疊層**用的畫布平面偏移（負＝比底色層**近** ⇒ 一定畫在它上面）。
        ///
        /// 菜單／設定／對話這些在真相是**遮罩層**（`.overlay-active`：底下的畫面留著、蓋一層暗色再放卡片），
        /// 不是把整屏換掉。`AppShell` 建疊層時把這個設成負值，建完就歸零。
        /// </summary>
        public static float CanvasPlaneOffset;

        /// <summary>
        /// 照相機現在的長寬比重算畫布的尺寸與縮放（建畫布時、螢幕大小變了時：摺疊屏開合、轉向）。
        /// </summary>
        public static void FitToCamera(RectTransform rect, Camera camera)
        {
            // 按設計格縮放：1280 設計單位 ⇒ 相機的正交視高（＝立繪那一側的自然取景）。
            var visibleHeight = camera.orthographicSize * 2f;
            // 填滿視野：按寬度貼合（多出來的高度留給彈性列）；否則照設計格（離線出圖那條）。
            var fill = FillViewport && camera.aspect > 0f;
            var width = DesignWidth > 0f ? DesignWidth : Reference.x;
            var scale = fill ? visibleHeight * camera.aspect / width : visibleHeight / Reference.y;
            var size = fill ? new Vector2(width, visibleHeight / scale) : Reference;
            rect.sizeDelta = size;
            rect.localScale = new Vector3(scale, scale, 1f);
            LastCanvasSize = size;
        }

        public static Canvas NewCanvas(Camera camera, string name = "UiCanvas", Color? background = null)
        {
            var go = new GameObject(name, typeof(RectTransform));
            go.layer = UiLayer;
            var canvas = go.AddComponent<Canvas>();
            canvas.renderMode = RenderMode.WorldSpace;
            canvas.worldCamera = camera;
            LastCanvasCamera = camera;      // 截圖要拍這一台（見 LastCanvasCamera 的理由）

            var rect = (RectTransform)canvas.transform;
            // 掛在相機的正前方置中：這樣「畫布內的座標」就等於「設計座標」，與真相同一格。
            rect.SetParent(camera.transform, false);
            rect.localPosition = new Vector3(0f, 0f, PlaneDistance + CanvasPlaneOffset);
            rect.localRotation = Quaternion.identity;
            FitToCamera(rect, camera);

            var scaler = go.AddComponent<CanvasScaler>();
            scaler.uiScaleMode = CanvasScaler.ScaleMode.ConstantPixelSize;
            scaler.scaleFactor = 1f;

            // ⚠️ **畫布根節點本身就是整個版面的容器**（topbar 固定 55＋body weight 1 由它分配）。
            //    沒有這一層的話，子節點的尺寸只由自己的 RectTransform 錨點決定，而
            //    `LayoutElement`（weight／固定高度）**只對「有 LayoutGroup 的父節點」有效**
            //    ⇒ 症狀是 topbar 撐滿整個畫布、裡面每個文字物件高 1280（這一輪就是這樣）。
            var rootGroup = go.AddComponent<VerticalLayoutGroup>();
            rootGroup.spacing = 0f;
            rootGroup.childControlWidth = true;
            rootGroup.childControlHeight = true;
            rootGroup.childForceExpandWidth = true;
            rootGroup.childForceExpandHeight = false;
            // ⚠️ **`.app-shell { border: 1px solid #9cb9d0 }` 是 border-box**（2026-10-12 抓到，系統性）：
            //    畫布根節點就是外殼那一格 ⇒ 裡面每一層都要**整圈內縮 1px**。少了這一行，
            //    各屏的東西全部比真相高／左 1px（量到的症狀：topbar 下緣、composer 上緣、外殼最外那一列）。
            rootGroup.padding = new RectOffset((int)DesignTokens.ShellEdgeWidth, (int)DesignTokens.ShellEdgeWidth,
                                               (int)DesignTokens.ShellEdgeWidth, (int)DesignTokens.ShellEdgeWidth);

            if (background.HasValue)
            {
                Backdrop("Background", rect, background.Value);
            }

            // 最外圈那條 1px 的外框（`border: 1px solid #9cb9d0`；圓角 12）。
            // 畫在底色之上、內容之下；`ApplyShellHoles` 會把白環（inset 3px）與各條底色排到它前後去。
            var edge = NewRect("ShellEdge", rect);
            edge.gameObject.AddComponent<LayoutElement>().ignoreLayout = true;
            var edgeImage = edge.gameObject.AddComponent<Image>();
            edgeImage.raycastTarget = false;
            edgeImage.sprite = RingSprite(Mathf.RoundToInt(DesignTokens.ShellCorner),
                                          Mathf.RoundToInt(DesignTokens.ShellEdgeWidth));
            edgeImage.type = Image.Type.Sliced;
            edgeImage.color = DesignTokens.ShellEdge;
            Stretch(edge);

            // ⚠️ **舞台底色不在這裡做**（2026-10-14 拆掉兩版）：① 第二張畫布會把立繪整個蓋掉；
            //    ② 一片幾何則要 `Shader.Find` 一個 shader，而**建置會把它裁掉** ⇒
            //    `new Material(null)` 直接拋 ⇒ 整屏建不起來（真機上 UI 全不見）。
            //    正解＝owner 指的「**UI 先鋪底、模型單獨一層**」：底色是 UI 自己的一個 Image
            //    （用畫布本來就有的 UI 材質，不必找 shader），模型那一層走 RenderTexture 貼進 UI 的格子裡
            //    （Next 的 3D 立繪就是這樣：`NextStage3D`）。
            return canvas;
        }


        /// <summary>建一個空的 `RectTransform` 節點。</summary>
        public static RectTransform NewRect(string name, Transform parent)
        {
            var go = new GameObject(name, typeof(RectTransform));
            go.layer = UiLayer;          // 我們建的節點**全部**是 UI（見 UiLayer 的理由）
            var rect = (RectTransform)go.transform;
            rect.SetParent(parent, false);
            return rect;
        }


        /// <summary>邊線貼哪一邊（對照 CSS 的 `border-bottom`／`border-left`）。</summary>
        public enum Edge { Top, Bottom, Left, Right }

        /// <summary>
        /// 畫一條邊線（貼著父容器某一邊、**不參與版面**、畫在底色之上）。
        ///
        /// 真相那一側大量用「一條線」表達語氣與分區：`.topbar` 下緣 2px `#90d5f1`、
        /// `.speech-bubble` 左緣 3px `#60cdef`、`.stage-index` 左緣 3px 藍線。
        /// UGUI 的 `Image` 沒有 border，所以用一條貼邊的細長矩形表示（比拉九宮格圖簡單，
        /// 數值也能跟 CSS 一字不差）。
        /// </summary>
        public static RectTransform EdgeLine(string name, Transform parent, Color color, float thickness, Edge edge)
        {
            var rect = NewRect(name, parent);
            switch (edge)
            {
                case Edge.Bottom:
                    rect.anchorMin = new Vector2(0f, 0f);
                    rect.anchorMax = new Vector2(1f, 0f);
                    rect.pivot = new Vector2(0.5f, 0f);
                    rect.offsetMin = Vector2.zero;
                    rect.offsetMax = Vector2.zero;
                    rect.sizeDelta = new Vector2(0f, thickness);
                    break;
                case Edge.Top:
                    rect.anchorMin = new Vector2(0f, 1f);
                    rect.anchorMax = new Vector2(1f, 1f);
                    rect.pivot = new Vector2(0.5f, 1f);
                    rect.offsetMin = Vector2.zero;
                    rect.offsetMax = Vector2.zero;
                    rect.sizeDelta = new Vector2(0f, thickness);
                    break;
                case Edge.Left:
                    rect.anchorMin = new Vector2(0f, 0f);
                    rect.anchorMax = new Vector2(0f, 1f);
                    rect.pivot = new Vector2(0f, 0.5f);
                    rect.offsetMin = Vector2.zero;
                    rect.offsetMax = Vector2.zero;
                    rect.sizeDelta = new Vector2(thickness, 0f);
                    break;
                default:   // Right
                    rect.anchorMin = new Vector2(1f, 0f);
                    rect.anchorMax = new Vector2(1f, 1f);
                    rect.pivot = new Vector2(1f, 0.5f);
                    rect.offsetMin = Vector2.zero;
                    rect.offsetMax = Vector2.zero;
                    rect.sizeDelta = new Vector2(thickness, 0f);
                    break;
            }
            var image = rect.gameObject.AddComponent<Image>();
            image.color = color;
            image.raycastTarget = false;
            rect.gameObject.AddComponent<Decor>();   // 純裝飾：命中判定跳過（它會 SetAsLastSibling ⇒ 否則永遠贏）
            var element = rect.gameObject.AddComponent<LayoutElement>();
            element.ignoreLayout = true;
            rect.SetAsLastSibling();       // 線要在底色之上（否則下緣線會被底色蓋掉）
            return rect;
        }

        /// <summary>
        /// **CSS 斜切漸層底**（`.tech-button` 的 `linear-gradient(125deg, #f9fcff 0 75%, #d7f3fc 75%)`）。
        ///
        /// 為什麼要現算：CSS 的漸層角度**相對元素自己的框**（交界線斜率 ＝ tan(θ−90°)；125° ⇒ 0.7，
        /// 與像素實測的交界斜率一致），所以不能烤一張固定貼圖去拉——按尺寸算、按尺寸快取。
        /// 座標：CSS 的 0° ＝朝上、順時針 ⇒ 方向 `(sinθ, −cosθ)`，漸層線長 `|w·sinθ| ＋ |h·cosθ|`；
        /// 著色點在中點 ± `stop` 的比例上。
        /// </summary>
        public static Texture2D DiagonalGradient(int width, int height, Color a, Color b,
                                                 float degrees, float stop)
        {
            // 解析度照螢幕密度（交界那條斜線才不會被放大成階梯），上限避免大面積吃記憶體。
            var gk = GradientScale(width, height);
            width *= gk;
            height *= gk;
            var key = $"{width}x{height}_{degrees}_{stop}";
            var cached = CachedGradient(key);
            if (cached != null) return cached;
            var tex = new Texture2D(width, height, TextureFormat.RGBA32, false);
            var rad = degrees * Mathf.Deg2Rad;
            var dx = Mathf.Sin(rad);
            var dy = -Mathf.Cos(rad);
            var length = Mathf.Abs(width * dx) + Mathf.Abs(height * dy);
            var cutoff = length * (stop - 0.5f);          // 相對盒中心
            var pixels = new Color[width * height];
            for (var y = 0; y < height; y++)
            {
                for (var x = 0; x < width; x++)
                {
                    var t = (x + 0.5f - width * 0.5f) * dx + (y + 0.5f - height * 0.5f) * dy;
                    // Unity 的貼圖是**由下往上** ⇒ 螢幕座標（y 往下）要翻過來
                    // 交界抗鋸齒：跨 1 texel 線性過渡（先前是 0/1 硬切 ⇒ 斜線一格一格的階梯）。
                    pixels[(height - 1 - y) * width + x] = Color.Lerp(a, b, Mathf.Clamp01(t - cutoff + 0.5f));
                }
            }
            tex.SetPixels(pixels);
            tex.filterMode = FilterMode.Bilinear;
            tex.wrapMode = TextureWrapMode.Clamp;
            tex.Apply();
            CacheGradient(key, tex);
            return tex;
        }

        /// <summary>多停點漸層在 `p`（0~1）上的顏色（硬邊＝區間寬 0 ⇒ 取後一顆）。</summary>
        private static Color StopColor((float At, Color Color)[] stops, float p)
        {
            for (var i = 0; i < stops.Length - 1; i++)
            {
                if (p < stops[i].At) return stops[i].Color;
                if (p <= stops[i + 1].At)
                {
                    var span = stops[i + 1].At - stops[i].At;
                    return span <= 0.0001f
                        ? stops[i + 1].Color
                        : Color.Lerp(stops[i].Color, stops[i + 1].Color, (p - stops[i].At) / span);
                }
            }
            return stops[stops.Length - 1].Color;
        }

        /// <summary>漸層貼圖的倍率：照螢幕密度，但整張不超過約 200 萬 texel（整片底色那種大面積就少放大一點）。</summary>
        private static int GradientScale(int width, int height)
        {
            var k = TexelScale;
            while (k > 1 && (long)width * height * k * k > 2_000_000L) k--;
            return k;
        }

        /// <summary>
        /// 漸層貼圖快取（key ＝ 尺寸＋角度＋停點）。**軟上限＋LRU**（外部評審 v2 C1）：固定解析度下只有十來張，
        /// 但旋轉／分割畫面／改字級會讓每個新尺寸各烘一張、舊的永遠不放 ⇒ 顯存只漲不跌。
        ///
        /// ⚠️ 不能單純「超過就丟最舊的」：貼圖還掛在 `RawImage` 上時 `Destroy` ⇒ 那塊底當場變白。
        /// 所以超過上限才掃一次場上的 `RawImage`（這些貼圖**只**給 `RawImage.texture` 用），
        /// 只回收**沒人在用**的、由最久沒取的開始；全都在用就先留著（上限是軟的），隔 16 張再掃。
        /// </summary>
        private static readonly Dictionary<string, (Texture2D Tex, long Used)> _gradients = new();
#if BA_SELFTEST
        private const int GradientSoftCap = 8;      // 自測包：上限壓低，讓回收那條路在真機上真的走到
#else
        private const int GradientSoftCap = 48;
#endif
        private static long _gradientClock;
        private static int _gradientTrimAt = GradientSoftCap;

        private static Texture2D CachedGradient(string key)
        {
            if (!_gradients.TryGetValue(key, out var e) || e.Tex == null) return null;
            _gradients[key] = (e.Tex, ++_gradientClock);
            return e.Tex;
        }

        private static void CacheGradient(string key, Texture2D tex)
        {
            _gradients[key] = (tex, ++_gradientClock);
            if (_gradients.Count > _gradientTrimAt) TrimGradients(key);
        }

        private static void TrimGradients(string fresh)
        {
            var inUse = new HashSet<Texture>();
            foreach (var raw in UnityEngine.Object.FindObjectsByType<RawImage>(FindObjectsInactive.Include,
                                                                               FindObjectsSortMode.None))
            {
                if (raw.texture != null) inUse.Add(raw.texture);
            }
            var idle = new List<(string Key, Texture2D Tex, long Used)>();
            foreach (var kv in _gradients)
            {
                if (kv.Key == fresh) continue;                         // 剛烘的這張還沒掛上去
                if (kv.Value.Tex == null) idle.Add((kv.Key, null, long.MinValue));          // 已被別處銷毀
                else if (!inUse.Contains(kv.Value.Tex)) idle.Add((kv.Key, kv.Value.Tex, kv.Value.Used));
            }
            idle.Sort((a, b) => a.Used.CompareTo(b.Used));
            var before = _gradients.Count;
            foreach (var (key, tex, _) in idle)
            {
                if (_gradients.Count <= GradientSoftCap * 3 / 4) break;
                _gradients.Remove(key);
                if (tex != null) UnityEngine.Object.Destroy(tex);
            }
            _gradientTrimAt = Mathf.Max(GradientSoftCap, _gradients.Count + 16);
            Debug.Log($"[ui] 漸層快取：{before} → {_gradients.Count} 張（在用 {inUse.Count}）");
        }

        /// <summary>
        /// **CSS 斜切漸層（多停點版）**——`.overlay-head` 的
        /// `linear-gradient(115deg, #d8f0fa 0 22%, #f9fcff 22% 78%, #edf8fe 100%)`：
        /// 兩條**硬邊**（22%／78%）＋末段漸變，2 色的 `DiagonalGradient` 畫不出來（它只給一條交界）。
        ///
        /// 座標與角度與 2 色版**同一條式子**（`dx = sinθ`、`dy = −cosθ`、投影長 `|w·dx|＋|h·dy|`）
        /// ⇒ 交界線斜率一致（125° 那顆鈕的交界已用像素驗過 0.7＝tan35°）。
        /// `stops` ＝ `(位置 0~1, 顏色)`、位置**遞增**；同一位置上放兩顆不同色＝硬邊。
        /// </summary>
        public static Texture2D DiagonalGradientStops(int width, int height, float degrees,
                                                      (float At, Color Color)[] stops, float radius = 0f)
        {
            var gk = GradientScale(width, height);
            width *= gk;
            height *= gk;
            radius *= gk;
            var key = $"s{width}x{height}_{degrees}_{radius:0.##}_" + string.Join(",",
                System.Array.ConvertAll(stops, s => $"{s.At:0.###}:{ColorUtility.ToHtmlStringRGBA(s.Color)}"));
            var cached = CachedGradient(key);
            if (cached != null) return cached;
            var tex = new Texture2D(width, height, TextureFormat.RGBA32, false);
            var rad = degrees * Mathf.Deg2Rad;
            var dx = Mathf.Sin(rad);
            var dy = -Mathf.Cos(rad);
            var length = Mathf.Abs(width * dx) + Mathf.Abs(height * dy);
            var pixels = new Color[width * height];
            for (var y = 0; y < height; y++)
            {
                for (var x = 0; x < width; x++)
                {
                    var t = (x + 0.5f - width * 0.5f) * dx + (y + 0.5f - height * 0.5f) * dy;
                    var p = Mathf.Clamp01((t + length * 0.5f) / Mathf.Max(0.0001f, length));
                    var c = StopColor(stops, p);
                    // 硬停點（同一位置兩色）抗鋸齒：離交界不到 1 texel 時，兩側的顏色按距離混
                    // （先前直接取後一顆 ⇒ 斜的交界線放大後是階梯）。
                    var texel = 1f / Mathf.Max(0.0001f, length);
                    for (var i = 0; i < stops.Length - 1; i++)
                    {
                        if (stops[i + 1].At - stops[i].At > 0.0001f) continue;
                        var edge = stops[i].At;
                        var d = (p - edge) / texel;               // 離交界幾個 texel（帶正負）
                        if (Mathf.Abs(d) >= 1f) continue;
                        var before = StopColor(stops, Mathf.Clamp01(edge - texel * 0.01f));
                        var after = StopColor(stops, Mathf.Clamp01(edge + texel * 0.01f));
                        c = Color.Lerp(before, after, Mathf.Clamp01(d + 0.5f));
                        break;
                    }
                    // 圓角遮罩（`border-radius`）：離角上那個圓心太遠 ⇒ 覆蓋率 0。
                    //   用覆蓋率（不是 0/1）才不會留階梯——半徑 4 的角在 720 寬的輸出上約 6px。
                    if (radius > 0f)
                    {
                        var mx = Mathf.Min(x + 0.5f, width - (x + 0.5f));
                        var my = Mathf.Min(y + 0.5f, height - (y + 0.5f));
                        var dx0 = radius - Mathf.Min(mx, radius);
                        var dy0 = radius - Mathf.Min(my, radius);
                        c.a *= Mathf.Clamp01(radius - Mathf.Sqrt(dx0 * dx0 + dy0 * dy0) + 0.5f);
                    }
                    // Unity 的貼圖是**由下往上** ⇒ 螢幕座標（y 往下）要翻過來
                    pixels[(height - 1 - y) * width + x] = c;
                }
            }
            tex.SetPixels(pixels);
            tex.filterMode = FilterMode.Bilinear;
            tex.wrapMode = TextureWrapMode.Clamp;
            tex.Apply();
            CacheGradient(key, tex);
            return tex;
        }

        /// <summary>
        /// **人字紋箭頭**（原生 `<select>` 的下拉箭頭）＝兩根細桿各轉 ±`atan(drop/halfWidth)`，
        /// **頂點朝下**（`∨`：左桿往右下降、右桿往左下降）。
        ///
        /// 形狀是從真相的像素直接量來的（音訊頁 `#input-language`：盒 x 125..424、箭頭 x 411..419、
        /// y 342..348、色 ≈ `#183c66`）——不是猜的三角形。**方向也在同一處量死**：真相那一欄
        /// 由 y 342（寬 412..419）收到 y 347（寬 415..416）⇒ 頂點在**下**（2026-10-12 才量；
        /// 先前兩根的轉向寫反 ⇒ 畫成 `∧`，設定頁六顆 `<select>` 的箭頭一直是倒的）。
        /// `center` ＝ 人字紋的**外接中心**、相對 `parent` 的**右上角**
        /// （`anchorMin = anchorMax = (1, 0.5)`）⇒ `center.y` 是**像素位移**、不是標準化錨值
        /// （要正好置中就給 0）。
        /// </summary>
        public static void Chevron(string name, RectTransform parent, Color color, Vector2 center,
                                   float halfWidth, float drop, float thickness)
        {
            for (var i = 0; i < 2; i++)
            {
                var bar = NewRect(name + i, parent);
                // ⚠️ **一定要 `ignoreLayout`**：父節點常常是 LayoutGroup（設定頁那顆是 `Row` 裡的
                //    `<select>` 盒）⇒ 不標的話，兩根桿會被當成版面列、尺寸被壓成 **0×0**
                //    （實測 2026-10-12：`SelectArrow0/1` dump 出 `rect 426,344 0x0` ⇒
                //    設定頁六個 `<select>` 的原生人字紋**從來沒畫出來過**，像素掃過去整片沒有）。
                bar.gameObject.AddComponent<LayoutElement>().ignoreLayout = true;
                bar.anchorMin = bar.anchorMax = new Vector2(1f, 0.5f);
                bar.pivot = new Vector2(0.5f, 0.5f);
                var length = Mathf.Sqrt(halfWidth * halfWidth + drop * drop);
                bar.sizeDelta = new Vector2(length, thickness);
                bar.anchoredPosition = new Vector2(center.x + (i == 0 ? -halfWidth : halfWidth) * 0.5f, center.y);
                var angle = Mathf.Atan2(drop, halfWidth) * Mathf.Rad2Deg;
                // 左桿（i=0）轉 **−angle**（順時針 ⇒ 右端下降）、右桿轉 **+angle** ⇒ 兩根在**下方**交會。
                bar.localRotation = Quaternion.Euler(0f, 0f, i == 0 ? -angle : angle);
                var image = bar.gameObject.AddComponent<Image>();
                image.color = color;
                image.raycastTarget = false;
            }
        }

        /// <summary>讓這個節點撐滿父容器。</summary>
        public static void Stretch(RectTransform rect)
        {
            rect.anchorMin = Vector2.zero;
            rect.anchorMax = Vector2.one;
            rect.offsetMin = Vector2.zero;
            rect.offsetMax = Vector2.zero;
        }

        /// <summary>
        /// **絕對定位的疊層**：撐滿父容器但**不參與版面**（`ignoreLayout`）。
        ///
        /// 用途＝CSS 的 `position: absolute`（`.profile-tag`、`.stage-serial`、`.stage-index` 那些）。
        /// 為什麼需要它：它們的父容器是 LayoutGroup ⇒ 一般的子節點會被**當成一列**排掉
        /// （實測：名牌被拉成滿寬的一列、高度也跑掉）。放進這一層之後，裡面就能用錨點照 CSS 定位。
        /// </summary>
        public static RectTransform Overlay(string name, Transform parent)
        {
            var rect = NewRect(name, parent);
            Stretch(rect);
            var element = rect.gameObject.AddComponent<LayoutElement>();
            element.ignoreLayout = true;
            return rect;
        }

        /// <summary>
        /// 鋪一層底色（撐滿父容器、**不參與版面**、畫在該容器最底下）。
        ///
        /// 為什麼要 `ignoreLayout`：它在版面裡是「背景」不是「一列」，沒關掉的話
        /// `VerticalLayoutGroup` 會把它當成一個子節點多分一段高度（實測會整個版面走鐘）。
        /// 「哪幾列有底色」就是這樣逐列決定的——**舞台那一列刻意不鋪**，好讓立繪透出來。
        /// </summary>
        public static RectTransform Backdrop(string name, Transform parent, Color color, float extendTop = 0f,
                                             float radius = 0f)
        {
            var rect = NewRect(name, parent);
            Stretch(rect);
            // `extendTop`：把底色往上多長一截，蓋掉「列距」那條縫。
            // 為什麼要這樣：列距是真相的數值（gap 7），但立繪是「UI 蓋在上面」的合成方式
            // ⇒ 縫裡會漏出立繪（實測：椅子從訊息列與輸入列之間的縫露出來）。
            // 版面數值照抄、只有底色多吃 7px，視覺上就是無縫的。
            if (extendTop > 0f) rect.offsetMax = new Vector2(rect.offsetMax.x, rect.offsetMax.y + extendTop);
            var image = rect.gameObject.AddComponent<Image>();
            image.color = color;
            image.raycastTarget = false;
            Round(image, radius);
            var element = rect.gameObject.AddComponent<LayoutElement>();
            element.ignoreLayout = true;
            rect.SetAsFirstSibling();
            return rect;
        }

        /// <summary>垂直容器（＝ `LinearLayout(orientation = VERTICAL)` ／ flex-direction: column）。</summary>
        public static RectTransform Column(string name, Transform parent, float spacing = 0f,
                                           RectOffset padding = null, bool stretch = true,
                                           TextAnchor align = TextAnchor.UpperLeft)
        {
            var rect = NewRect(name, parent);
            if (stretch) Stretch(rect);
            var group = rect.gameObject.AddComponent<VerticalLayoutGroup>();
            group.spacing = spacing;
            group.padding = padding ?? new RectOffset();
            group.childAlignment = align;
            group.childControlWidth = true;
            group.childControlHeight = true;
            group.childForceExpandWidth = true;
            group.childForceExpandHeight = false;
            return rect;
        }

        /// <summary>
        /// 水平容器（＝ `LinearLayout(orientation = HORIZONTAL)` ／ flex-direction: row）。
        ///
        /// ⚠️ **`stretch` 一定要預設開**：畫布根節點**沒有** LayoutGroup，子節點的尺寸與位置
        /// 就由它自己的 RectTransform 決定；不撐滿的話會留在 Unity 的預設值（100×100、置中）
        /// ⇒ 整條 topbar 會擠成畫面正中間的一小團（這一輪就是這樣：品牌名變成直排亂碼）。
        /// </summary>
        public static RectTransform Row(string name, Transform parent, float spacing = 0f,
                                        RectOffset padding = null, TextAnchor align = TextAnchor.MiddleLeft,
                                        bool stretch = true)
        {
            var rect = NewRect(name, parent);
            if (stretch) Stretch(rect);
            var group = rect.gameObject.AddComponent<HorizontalLayoutGroup>();
            group.spacing = spacing;
            group.padding = padding ?? new RectOffset();
            group.childAlignment = align;
            group.childControlWidth = true;
            group.childControlHeight = true;
            group.childForceExpandWidth = false;
            // ⚠️ **不要** forceExpandHeight：那會把每個子節點（例如 24px 高的 mode chip）拉滿整列
            //    高度、`LayoutElement` 的固定高度也一併失效（實測：chip 的文字高 55）。
            //    要撐高的子節點自己帶 `Weight`（flexibleHeight）就好。
            group.childForceExpandHeight = false;
            return rect;
        }

        /// <summary>
        /// <summary>
        /// **把版面結算乾淨**（無頭下沒有逐幀迴圈，一定要手動、而且要**由內而外多趟**）。
        ///
        /// 為什麼不能只呼叫一次 `LayoutRebuilder.ForceRebuildLayoutImmediate(root)`：
        /// 它對這棵子樹只跑**一趟**，而巢狀的 LayoutGroup（`Row` 裡面包 `Column` 再包 `Text`）
        /// 需要「子節點先算出 preferred，父容器才知道要給多少」⇒ 一趟只結算最外層，
        /// 深層節點停在 **0 高**（實測：葉子 `pref 83x9` 但 rect `301x0`）。
        /// 症狀是「文字 dump 全對、畫面上全部疊在一起」——很容易誤判成字型或數值問題。
        ///
        /// 做法：把子樹裡每個 LayoutGroup **依深度由深到淺**逐個強制重建，最後再重建根。
        /// </summary>
        public static void Settle(RectTransform root)
        {
            Canvas.ForceUpdateCanvases();
            RebuildDeepFirst(root);
            Canvas.ForceUpdateCanvases();

            // ⚠️ 第二步：**量到就釘住**（measure-then-pin）。
            // 實測（葉子 `pref 83x9` 但 rect `301x0`）：`Row`（水平 LayoutGroup）的**交叉軸**分配
            // 不會把子節點的高度用上——量測值有、卻沒人消費，深層文字全停在 0 高（畫面上疊在一起）。
            // 無頭下沒有逐幀迴圈讓 UGUI 慢慢收斂，所以：把文字量到的 preferred 高度寫回它的
            // `minHeight`（min ≡ preferred ⇒ 任何分配路徑都只能給這個值），再重建一次。
            PinTextHeights(root);
            Canvas.ForceUpdateCanvases();
            RebuildDeepFirst(root);
            Canvas.ForceUpdateCanvases();
            ApplyGradientFills(root);       // 最後才烘：尺寸（寬高）這一步才算定
            ApplyTextWidthBars(root);       // 同理：底線寬要看文字量出來的寬
            ApplyShellHoles(root);          // 同理：外殼底色要圍著**舞台結算後的框**留洞
            ApplySoftShadows(root);         // 同理：陰影要貼著**目標結算後**的框（見 ShadowBehind）
        }

        private static void RebuildDeepFirst(RectTransform root)
        {
            var groups = new List<RectTransform>();
            CollectGroups(root, groups);
            groups.Sort((a, b) => Depth(b).CompareTo(Depth(a)));   // 深的先算
            foreach (var group in groups) LayoutRebuilder.ForceRebuildLayoutImmediate(group);
            LayoutRebuilder.ForceRebuildLayoutImmediate(root);
        }

        /// <summary>把每個文字節點量到的 preferred 高度釘進 `LayoutElement.minHeight`（見 `Settle`）。</summary>
        ///
        /// ⚠️ **有 `LineBox` 的文字要用「行盒高度」＝ `行數 × (字級 × 比例)`，不是量到的 preferred**
        /// （owner 2026-10-11 拍板「A 動」）。
        /// 為什麼：legacy `Text` 的 `preferredHeight` 是**墨跡高**、不是行盒——實測 9px ⇒ 8、12px ⇒ 11，
        /// 而真相（瀏覽器）那一側是 CSS 行盒 9px ⇒ 14、12px ⇒ 16 ⇒ 每個文字塊都矮 3～6px、
        /// **逐格累積**（背景頁整片 −6、角色頁 −12）。行數取 `cachedTextGenerator.lineCount`
        /// （＝這條寬度下**真的折了幾行**，比拿 preferred 反推可靠；反推只當後備）。
        private static void PinTextHeights(Transform node)
        {
            foreach (Transform child in node)
            {
                if (child is RectTransform rect && child.GetComponent<Graphic>() != null)
                {
                    var text = child.GetComponent<TextMeshProUGUI>() as Graphic ?? child.GetComponent<Text>();
                    if (text != null)
                    {
                        var preferred = LayoutUtility.GetPreferredHeight(rect);
                        var box = child.GetComponent<LineBox>();
                        if (box != null && box.Size > 0f && box.Ratio > 0f)
                        {
                            var lines = 0;
                            if (text is Text legacy) lines = legacy.cachedTextGenerator.lineCount;
                            else if (text is TMPro.TMP_Text tmp && tmp.textInfo != null) lines = tmp.textInfo.lineCount;
                            if (lines <= 0) lines = Mathf.Max(1, Mathf.RoundToInt(preferred / box.LineHeight));
                            preferred = lines * box.LineHeight;
                        }
                        // ⚠️ **有內嵌貼圖（`<sprite>`）的文字例外**：那一行的行盒比字型行盒高
                        //    （`.sticker-inline` 22.75 ＞ 18.2）⇒ 拿「行數 × 行盒」反推會把它壓回字型
                        //    那一條（實測 2026-10-12：真相氣泡 39、Unity 33）。TMP 量到的 preferred
                        //    **已經把 sprite 的 ascent／descent 算進去**（`TMP_Text.cs` 的 sprite 分支用
                        //    sprite asset 的 faceInfo）⇒ 直接採用它。
                        if (text is TMPro.TMP_Text spriteText && spriteText.textInfo != null &&
                            !string.IsNullOrEmpty(spriteText.text) && spriteText.text.Contains("<sprite"))
                            preferred = LayoutUtility.GetPreferredHeight(rect);
                        if (preferred > 0.5f)
                        {
                            var element = rect.GetComponent<LayoutElement>() ??
                                          rect.gameObject.AddComponent<LayoutElement>();
                            if (element.minHeight < preferred) element.minHeight = preferred;
                            if (element.preferredHeight < preferred) element.preferredHeight = preferred;
                        }
                    }
                }
                PinTextHeights(child);
            }
        }

        /// <summary>
        /// 文字的行盒規格（`Text` 建節點時掛上、`Settle` 量高度時讀）——見 `DesignTokens.TextLineRatio`
        /// 與 `PinTextHeights` 的說明。`LineHeight` ＝ 這個字級想要的**一整行**高度（設計 px）。
        /// </summary>
        public sealed class LineBox : MonoBehaviour
        {
            public float Size;
            public float Ratio;
            public float LineHeight => Size * Ratio;
        }

        /// <summary>
        /// 斜切漸層底的規格（`Settle` 時按**結算後的實際尺寸**烘貼圖——見 `DiagonalGradient`）。
        ///
        /// 為什麼要等 `Settle`：CSS 的漸層交界線位置**取決於元素自己的寬高**（`.tech-button` 是
        /// `width: 100%`），建節點當下還不知道寬度 ⇒ 只能結算完再烘。
        /// </summary>
        public sealed class GradientFill : MonoBehaviour
        {
            public Color A;
            public Color B;
            public float Degrees;
            public float Stop;
        }

        /// <summary>多停點漸層的規格（`.overlay-head` 的 115° 三段底；見 `DiagonalGradientStops`）。</summary>
        public sealed class GradientStopsFill : MonoBehaviour
        {
            public float Degrees;
            public float[] At = System.Array.Empty<float>();
            public Color[] Cols = System.Array.Empty<Color>();
            /// <summary>圓角（CSS 的 `border-radius`）：> 0 ⇒ 貼圖的角要切掉（`.tile-icon` 是
            /// `border-radius: 4px` 的漸層底）。切掉的地方 alpha=0 ⇒ 露下面的卡底色（＝ CSS 的語意）。</summary>
            public float Radius;
        }

        /// <summary>
        /// **寬度跟著「旁邊那顆文字」走的底線**（`.overlay-head h2::after` 的 4px 黃短線：
        /// CSS 的 `h2` 是 `display: inline-block` ⇒ 底線寬＝**標題文字寬**，不是整條抬頭寬）。
        ///
        /// 為什麼不能建節點時就算：那時候還不知道文字多寬（要等 `Settle`）⇒ 這一條在 `Settle`
        /// 最後才寫 `sizeDelta.x`（＝父節點文字的 preferredWidth ＋ `Extra`）。
        /// 實測（2026-10-11 像素）：真相那條黃線只有 26 寬（x 217..243）、先前畫成**整片 436**
        /// （x 12..448）——差得看得出來，而文字 dump 一句話都不會說。
        /// </summary>
        public sealed class TextWidthBar : MonoBehaviour
        {
            public float Extra;
        }

        /// <summary>把 `GradientFill`／`GradientStopsFill` 的底按結算後的尺寸烘好（`Settle` 最後一步叫）。</summary>
        private static void ApplyGradientFills(Transform node)
        {
            foreach (Transform child in node)
            {
                if (child is RectTransform rect)
                {
                    var raw = child.GetComponent<RawImage>();
                    var spec = child.GetComponent<GradientFill>();
                    var stops = child.GetComponent<GradientStopsFill>();
                    if (raw != null && (spec != null || (stops != null && stops.At.Length >= 2)))
                    {
                        var w = Mathf.Max(1, Mathf.RoundToInt(rect.rect.width));
                        var h = Mathf.Max(1, Mathf.RoundToInt(rect.rect.height));
                        if (spec != null)
                        {
                            raw.texture = DiagonalGradient(w, h, spec.A, spec.B, spec.Degrees, spec.Stop);
                        }
                        else
                        {
                            var list = new (float, Color)[stops.At.Length];
                            for (var i = 0; i < list.Length; i++) list[i] = (stops.At[i], stops.Cols[i]);
                            raw.texture = DiagonalGradientStops(w, h, stops.Degrees, list, stops.Radius);
                        }
                    }
                }
                ApplyGradientFills(child);
            }
        }

        /// <summary>
        /// **把「寬度跟著父文字」的底線量好**（`TextWidthBar`；`Settle` 最後一步叫——要在尺寸都定下來之後）。
        /// 父節點沒有文字、或量到的寬是 0 就原樣留著（不猜）。
        /// </summary>
        private static void ApplyTextWidthBars(Transform node)
        {
            foreach (Transform child in node)
            {
                if (child is RectTransform rect && rect.parent is RectTransform parent)
                {
                    var spec = child.GetComponent<TextWidthBar>();
                    if (spec != null)
                    {
                        var hasText = parent.GetComponent<TextMeshProUGUI>() != null ||
                                      parent.GetComponent<Text>() != null;
                        var width = hasText ? LayoutUtility.GetPreferredWidth(parent) : 0f;
                        if (width > 0.5f) rect.sizeDelta = new Vector2(width + spec.Extra, rect.sizeDelta.y);
                    }
                }
                ApplyTextWidthBars(child);
            }
        }

        private static void CollectGroups(Transform node, List<RectTransform> into)
        {
            if (node is RectTransform rect && node.GetComponent<UnityEngine.UI.LayoutGroup>() != null)
                into.Add(rect);
            foreach (Transform child in node) CollectGroups(child, into);
        }

        private static int Depth(Transform node)
        {
            var depth = 0;
            for (var cursor = node.parent; cursor != null; cursor = cursor.parent) depth++;
            return depth;
        }

        /// <summary>這一格佔多少「權重」——**對照真相的 weight／flex**（例如立繪 1：訊息 .52）；`min` 對照 CSS 的 `minmax(min, Xfr)`。</summary>
        public static LayoutElement Weight(RectTransform rect, float weight = 1f, float min = 0f)
        {
            var element = rect.GetComponent<LayoutElement>() ?? rect.gameObject.AddComponent<LayoutElement>();
            element.flexibleWidth = weight;
            element.flexibleHeight = weight;
            element.minHeight = min;
            return element;
        }

        /// <summary>
        /// **等寬欄**（＝ CSS grid 的 `1fr 1fr`）。
        ///
        /// ⚠️ 只用 <see cref="Weight"/>（`flexibleWidth = 1`）**做不到等寬**：UGUI 的分配是
        /// `preferred + 剩餘空間 × flexible/Σflexible` ⇒ **preferred 比較寬的那一格最後還是比較寬**。
        /// 實測（選單的四塊磚、`Row` 的 `childForceExpandWidth = false`）：兩欄變成 **203／190**
        /// ⇒ 第二欄的標籤右移 5～11px（真相 `1fr 1fr` ⇒ 各 196.5）。把 `preferredWidth` 歸零之後
        /// 寬度＝剩餘空間平均分 ⇒ **等寬**（與 `childForceExpandWidth = true` 的語意一致）。
        /// 高度不動（`flexibleHeight = 0` ⇒ 照內容／自己帶的 `FixedHeight`）。
        /// </summary>
        public static LayoutElement EqualWidth(RectTransform rect, float weight = 1f)
        {
            var element = rect.GetComponent<LayoutElement>() ?? rect.gameObject.AddComponent<LayoutElement>();
            element.minWidth = 0f;
            element.preferredWidth = 0f;
            element.flexibleWidth = weight;
            element.flexibleHeight = 0f;
            return element;
        }

        /// <summary>
        /// <summary>
        /// 只吃**寬**的權重（＝ CSS 在**單軸**上的 `flex: 1 1 auto`：寬撐滿、**高照內容**）。
        ///
        /// 為什麼需要它：<see cref="Weight"/> 一次設 `flexibleWidth` ＋ `flexibleHeight`，而
        /// **`flexibleHeight &gt; 0` 的那一格會在父容器有剩餘高度時把它吃掉**（LayoutGroup 依
        /// flexibleHeight 分配多出來的高度）。實測（TRPG 那一屏）：`#trpg-words` 裡那一句話的底色
        /// 用 `Weight` ⇒ 變成**橫跨整格、幾乎佔滿卡片內部的米色大塊**（真相是貼著那一行的小標籤；
        /// 文字 dump 全綠、只有像素量測／並排圖看得出來）。
        /// 「寬撐滿、高照內容」的格子一律用這一支。
        /// </summary>
        public static LayoutElement WeightWidth(RectTransform rect, float weight = 1f)
        {
            var element = rect.GetComponent<LayoutElement>() ?? rect.gameObject.AddComponent<LayoutElement>();
            element.flexibleWidth = weight;
            element.flexibleHeight = 0f;
            return element;
        }

        /// <summary>
        /// **控制項的值**（不比對文字、另外比對的那一路）。
        ///
        /// 為什麼需要它：真值那一側 `<input>`／`<select>` 的**值不進 `innerText`**
        /// （`<input>` 的 value 根本不算文字節點；`<select>` 只吐選項、不吐選中那一項），
        /// 所以純文字對拍天生看不到「現在選的是什麼」。做法：畫一個**停用的**文字節點
        /// （`enabled = false` ⇒ UGUI 不畫、不佔版面），名字帶 `#control:` 前綴，
        /// `Dump()` 會把它單獨輸出成 `#control:<key>|<值>` 一行 ⇒ 由 `ab_compare.py` 分別比。
        /// </summary>
        public static void ControlValue(Transform parent, string key, string value)
        {
            var rect = NewRect("#control:" + key, parent);
            var text = rect.gameObject.AddComponent<Text>();
            text.text = value ?? string.Empty;
            text.enabled = false;                     // 不畫、但 dump 讀得到
            var element = rect.gameObject.AddComponent<LayoutElement>();
            element.ignoreLayout = true;
            Stretch(rect);
        }

        /// <summary>
        /// 把一塊 `Image` 變成**圓形**（棋子／星位用）。
        ///
        /// 為什麼要程序生成：UGUI 的 `Image` 沒有 sprite 就是一塊方塊，而 URP 下我們不引任何貼圖資產
        /// ⇒ 用一張 64×64 的圓形遮罩當 sprite（白，實際顏色走 `Image.color` 的 tint）。
        /// 只在第一次呼叫時做一張，之後共用。
        /// </summary>
        /// <summary>
        /// **程序貼圖的解析度倍率**：1 個設計單位畫成幾個 texel。
        ///
        /// ⚠️ 2026-10-19（owner：「曲線元素的鋸齒感很強」）：先前所有程序生成的形狀（圓角、外框環、
        /// 圓、斜切漸層的交界）都是 **1 texel ＝ 1 設計單位**，而手機上 1 個設計單位要畫成約 2.6 個
        /// 實體像素（460 寬的設計格鋪滿 1216 寬的螢幕）⇒ 每條邊都是低解析度貼圖被放大 2.6 倍：
        /// 硬邊的（`Circle`、漸層的硬停點）變成 2~3 px 的階梯，抗鋸齒過的則變糊。
        /// 改成照**螢幕實際密度**生成（這台＝3），sprite 的 `pixelsPerUnit` 同倍放大 ⇒ 版面尺寸不變。
        /// </summary>
        public static int TexelScale
        {
            get
            {
                if (_texelScale > 0) return _texelScale;
                var perUnit = Screen.width / Mathf.Max(1f, DesignTokens.Reference.x);
                _texelScale = Mathf.Clamp(Mathf.CeilToInt(perUnit - 0.05f), 1, 4);
                Debug.Log($"[screen] 程序貼圖倍率 ×{_texelScale}（螢幕 {Screen.width}px ÷ 設計 {DesignTokens.Reference.x:0}）");
                return _texelScale;
            }
        }

        private static int _texelScale;

        public static void Circle(Image image)
        {
            if (_circleSprite == null)
            {
                // 256 見方 ＋ **解析式抗鋸齒**（覆蓋率＝半徑 − 距離，跨 1 texel 的過渡）＋ mipmap：
                // 最大的圓（☰ 39 設計單位 ≈ 103 px）也不必放大；小圓點靠 mipmap 縮小不閃。
                // ⚠️ 先前是 64 見方、**0/1 硬邊** ⇒ 真機上整圈階梯（☰、狀態點、撥桿旋鈕、棋子都是它）。
                const int size = 256;
                var radius = size / 2f - 1f;
                var texture = new Texture2D(size, size, TextureFormat.RGBA32, true);
                var pixels = new Color32[size * size];
                for (var y = 0; y < size; y++)
                {
                    for (var x = 0; x < size; x++)
                    {
                        var dx = x + 0.5f - size / 2f;
                        var dy = y + 0.5f - size / 2f;
                        var cover = Mathf.Clamp01(radius - Mathf.Sqrt(dx * dx + dy * dy) + 0.5f);
                        pixels[y * size + x] = new Color32(255, 255, 255, (byte)Mathf.RoundToInt(255f * cover));
                    }
                }
                texture.SetPixels32(pixels);
                texture.filterMode = FilterMode.Trilinear;
                texture.wrapMode = TextureWrapMode.Clamp;
                texture.Apply(true);
                _circleSprite = Sprite.Create(texture, new Rect(0f, 0f, size, size), new Vector2(0.5f, 0.5f));
                _circleSprite.name = "UiCircle";
            }
            image.sprite = _circleSprite;
            image.type = Image.Type.Simple;
        }

        /// <summary>
        /// 一條 1px 的外框。UGUI 的 `Image` 沒有 border ⇒ 四條貼邊的線（`EdgeLine` 是 `ignoreLayout`，
        /// 不會被版面當成一列）。選單與五子棋那兩屏都要框，所以在這裡只寫一份。
        ///
        /// ⚠️ **圓角元素（`radius > 0`）不能用這四條直線**：直線是方的，會在四個角**刺出圓弧外面**
        /// （真相那一圈是跟著圓弧走的）⇒ 改用一張**環形圓角 sprite**（`RingSprite`）貼成一個 child。
        /// </summary>
        public static void Border(RectTransform rect, Color color, float thickness, float radius = 0f)
        {
            if (radius > 0f)
            {
                var ring = NewRect("Border", rect);
                Stretch(ring);
                var element = ring.gameObject.AddComponent<LayoutElement>();
                element.ignoreLayout = true;
                var image = ring.gameObject.AddComponent<Image>();
                image.color = color;
                image.raycastTarget = false;
                image.sprite = RingSprite(Mathf.RoundToInt(radius), Mathf.Max(1, Mathf.RoundToInt(thickness)));
                image.type = Image.Type.Sliced;
                // ⚠️ **一定要標成裝飾**：這一圈的 RectTransform 是**整個面板那麼大**（中間透明），
                //    而命中判定看的是矩形、不是像素 ⇒ 不標的話它會把面板裡每一擊都吃掉
                //    （2026-10-20 實測：MomoTalk 檢索欄怎麼點都只命中 `Border ← TalkPanel`）。
                ring.gameObject.AddComponent<Decor>();
                return;
            }
            EdgeLine("BorderTop", rect, color, thickness, Edge.Top);
            EdgeLine("BorderBottom", rect, color, thickness, Edge.Bottom);
            EdgeLine("BorderLeft", rect, color, thickness, Edge.Left);
            EdgeLine("BorderRight", rect, color, thickness, Edge.Right);
        }

        /// <summary>
        /// **`.tech-button`／`.send-button`**（`styles.css` 261）：radius 4、1px `#91bfd7` 外框、
        /// **125° 斜切漸層底**（`#f9fdff 0 75%, #d7f3fc 75%`）、`box-shadow: 0 2px 0 #aac8d7`
        /// （＝下緣外側那條 2px 實線、**不佔版面**）、字 `#25587c` 置中不折行。
        ///
        /// 各處的覆寫值不一樣 ⇒ 由呼叫端給：設定頁那顆＝**32 高／字 11**（`styles.css 368`）；
        /// 對話屏條列模式那顆（`#chat-goto-live`）＝**31 高／字 10、寬吃內容**（
        /// `.chat-mode-bar button { flex: 0 0 auto; height: 31px; padding: 0 12px }`
        /// ⇒ 真相實測 103 ＝ 內容 79 ＋ 左右各 12）。
        ///
        /// 為什麼要共用一顆（2026-10-12）：先前只有設定頁有這一支、對話屏那顆是**一片平色**
        /// （`Panel(…, HexLocal("e8f4fb"))`）⇒ 少了 1px 外框、少了那條 2px 壓條、漸層也不是
        /// 125° 的那條切線（像素取樣：真相 1px 外框 `145,191,215` ＋ 底 `249,253,255`，
        /// 平色版整片 `232,244,251`）。數值各抄一份會漂。
        /// </summary>
        public static RectTransform TechButton(string name, RectTransform parent, string label,
                                               float height, float fontSize, float width = 0f)
        {
            var button = Row(name, parent, 0f, null, TextAnchor.MiddleLeft);
            FixedHeight(button, height);
            if (width > 0f) FixedWidth(button, width);
            var fill = NewRect("ButtonFill", button);
            // ⚠️ 這一格必須 `ignoreLayout`：父節點是 LayoutGroup，**它會蓋掉子節點自己的 anchors**
            //    ⇒ 只 `Stretch()` 的話結算出來是 `0x0`（實測 2026-10-11：ButtonFill rect 124,509 0x0、
            //    鈕底整片純白，像素才看得出來）。
            fill.gameObject.AddComponent<LayoutElement>().ignoreLayout = true;
            Stretch(fill);
            var raw = fill.gameObject.AddComponent<RawImage>();
            raw.raycastTarget = false;
            var spec = fill.gameObject.AddComponent<GradientFill>();
            spec.A = DesignTokens.TechButtonBgA;
            spec.B = DesignTokens.TechButtonBgB;
            spec.Degrees = DesignTokens.TechButtonAngle;
            spec.Stop = DesignTokens.TechButtonStop;
            Border(button, DesignTokens.TechButtonLine, 1f, DesignTokens.TechButtonRadius);
            // `box-shadow: 0 2px 0 #aac8d7`——陰影**不佔版面**（CSS 的 shadow 也一樣）⇒ 掛疊層、
            // 貼在盒子的下緣外側 2px，不會蓋到下面那圈 1px 外框。
            var shadowHost = Overlay("ButtonShadow", button);
            var shadow = NewRect("ShadowStrip", shadowHost);
            shadow.anchorMin = new Vector2(0f, 0f);
            shadow.anchorMax = new Vector2(1f, 0f);
            shadow.pivot = new Vector2(0.5f, 1f);
            shadow.sizeDelta = new Vector2(0f, DesignTokens.TechButtonShadow);
            shadow.anchoredPosition = Vector2.zero;
            var shadowImage = shadow.gameObject.AddComponent<Image>();
            shadowImage.color = DesignTokens.TechButtonShadowColor;
            shadowImage.raycastTarget = false;
            var text = Text("ButtonLabel", button, label, fontSize, DesignTokens.TechButtonText,
                            TextAlignmentOptions.Center, noWrap: true, bold: true);   // `.tech-button`
            // 置中＝吃滿整顆鈕的寬（`text-align: center` ＋ `width: 100%`）；只吃寬、不吃高
            // （`Weight` 會連 `flexibleHeight` 一起設 ⇒ 文字被拉高，見 `LabelWeightWidth` 的說明）。
            WeightWidth((RectTransform)text.transform, 1f);
            return button;
        }

        // ── 圓角（＝ CSS 的 `border-radius`）──────────────────────────────────────
        //
        // 為什麼不是「畫四條弧線」而是**換一張 sprite**：UGUI 的 `Image` 用 **Sliced** 貼圖時，
        // 四個角由 sprite 的 `border` 決定、中央拉伸 ⇒ **一張 sprite 適用所有尺寸**——這正是 CSS
        // `border-radius` 的語意（同一條 `border-radius: 7px` 套在 100 寬與 400 寬的氣泡上）。
        //
        // 座標對齊：sprite 的 `pixelsPerUnit = 100`（＝ `CanvasScaler` 的 `referencePixelsPerUnit`）
        // ⇒ **sprite 的 1 px ＝ 設計框的 1 單位** ⇒ 畫出來的圓角半徑就是傳進來的 `radius`。
        // 邊緣用 4× 子像素超取樣（不然圓弧會有階梯——並排圖放大就看得到）。
        //
        // 緩存：以 (radius, thickness) 為鍵；一張 sprite 只用一次也無所謂（棋盤那一屏就有 5~6 種）。

        /// <summary>把一張 `Image` 變成圓角底（`border-radius: radius`，四角相同）。</summary>
        public static void Round(Image image, float radius) => RoundCorners(image, radius, radius, radius, radius);

        /// <summary>
        /// 四角可以不同（CSS 的 `border-radius: 14px 14px 0 0` 那種——底部升起來的表只有上面兩角是圓的）。
        ///
        /// 做法：sprite 的四角各用自己的半徑畫弧，`border` 切法統一切 **max(四角)** 那一格
        /// ⇒ 中央照樣可以任意拉伸。**直角（半徑 0）那一角就照直角畫**（＝ CSS 的語意）。
        /// </summary>
        public static void RoundCorners(Image image, float topLeft, float topRight, float bottomRight,
                                       float bottomLeft)
        {
            if (image == null) return;
            var biggest = Mathf.Max(Mathf.Max(topLeft, topRight), Mathf.Max(bottomRight, bottomLeft));
            if (biggest <= 0f) return;
            image.sprite = RoundSprite(Mathf.RoundToInt(bottomLeft), Mathf.RoundToInt(bottomRight),
                                       Mathf.RoundToInt(topRight), Mathf.RoundToInt(topLeft));
            image.type = Image.Type.Sliced;
        }

        private static readonly Dictionary<int, Sprite> _roundSprites = new Dictionary<int, Sprite>();

        /// <summary>
        /// **圓角裁切**（＝ CSS 的 `border-radius` 套在**圖片**上，不只是色塊）。
        ///
        /// 為什麼需要它：貼圖／縮圖是 `RawImage`（一張貼圖），換 sprite 那招沒用——要**裁**。
        /// 做法：父節點一張**白底圓角** `Image` ＋ `Mask`（`showMaskGraphic = true`）
        /// ⇒ 子節點被裁在那個圓角輪廓裡；而那層白底正好就是 CSS 的 `background: #fff`
        /// （`.sticker-cell img`／`.album-cell img` 都是「白底 ＋ `object-fit`」）。
        /// 回傳的是**要放圖的那一層**（子節點請自己 `Stretch`）。
        /// </summary>
        public static RectTransform RoundMask(string name, Transform parent, float radius, Color background)
        {
            var rect = NewRect(name, parent);
            var image = rect.gameObject.AddComponent<Image>();
            image.color = background;
            image.raycastTarget = false;
            if (radius > 0f)
            {
                Round(image, radius);
                var mask = rect.gameObject.AddComponent<Mask>();
                mask.showMaskGraphic = true;      // 白底本身要畫出來（＝ CSS 的 background）
            }
            return rect;
        }

        private static Sprite RoundSprite(int bl, int br, int tr, int tl) => CachedRounded(bl, br, tr, tl, 0);
        private static Sprite RingSprite(int radius, int thickness) =>
            CachedRounded(radius, radius, radius, radius, thickness);

        /// <summary>
        /// 一張圓角矩形（`thickness = 0`）／**環形**圓角矩形（`thickness > 0`：內緣往內挖一圈
        /// ⇒ 給「圓角 ＋ 1px 外框」用，見 `Border`）。
        ///
        /// 尺寸＝`2×max(四角) + 2`：中間那 2 px 是「直的」那一段 ⇒ `border = max` 的九宮格切法之下，
        /// 中央可以任意拉伸而四個角的圓弧不變形（＝ CSS 那條 `border-radius` 套在任何尺寸上）。
        /// 邊緣用 4× 子像素超取樣（不然圓弧會有階梯，並排圖一放大就看得到）。
        /// </summary>
        private static Sprite CachedRounded(int bl, int br, int tr, int tl, int thickness)
        {
            var biggest = Mathf.Max(Mathf.Max(bl, br), Mathf.Max(tr, tl));
            var key = ((Mathf.Max(1, biggest) * 64 + Mathf.Clamp(bl, 0, 63)) * 64 +
                       Mathf.Clamp(br, 0, 63)) * 64 + Mathf.Clamp(tl, 0, 63) + 1000000 * thickness;
            if (_roundSprites.TryGetValue(key, out var cached)) return cached;

            const int samples = 4;                       // 子像素取樣（邊緣平滑）
            // 貼圖照螢幕密度生成（見 `TexelScale`）：形狀在**設計單位**裡算，取樣格細 k 倍。
            var k = TexelScale;
            var size = Mathf.Max(1, biggest) * 2 + 2;     // 設計單位
            var texSize = size * k;                       // texel
            var texture = new Texture2D(texSize, texSize, TextureFormat.RGBA32, false);
            texture.wrapMode = TextureWrapMode.Clamp;
            var pixels = new Color32[texSize * texSize];
            for (var y = 0; y < texSize; y++)
            {
                for (var x = 0; x < texSize; x++)
                {
                    var cover = 0;
                    for (var sy = 0; sy < samples; sy++)
                    {
                        for (var sx = 0; sx < samples; sx++)
                        {
                            var px = (x + (sx + 0.5f) / samples) / k;
                            var py = (y + (sy + 0.5f) / samples) / k;
                            if (!InRounded(px, py, size, bl, br, tr, tl)) continue;
                            // 環形：內緣那個圓角矩形要挖掉（往內縮 thickness、四角半徑也跟著縮）。
                            if (thickness > 0 &&
                                InRounded(px - thickness, py - thickness, size - 2 * thickness,
                                          Mathf.Max(0, bl - thickness), Mathf.Max(0, br - thickness),
                                          Mathf.Max(0, tr - thickness), Mathf.Max(0, tl - thickness)))
                                continue;
                            cover++;
                        }
                    }
                    var alpha = (byte)Mathf.RoundToInt(255f * cover / (samples * samples));
                    pixels[y * texSize + x] = new Color32(255, 255, 255, alpha);
                }
            }
            texture.SetPixels32(pixels);
            texture.Apply();
            var border = Mathf.Max(1, biggest) * k;
            // `pixelsPerUnit` 同倍放大 ⇒ 九宮格的角在版面上仍是 `radius` 個設計單位。
            var sprite = Sprite.Create(texture, new Rect(0f, 0f, texSize, texSize), new Vector2(0.5f, 0.5f), 100f * k, 0,
                                       SpriteMeshType.FullRect,
                                       new Vector4(border, border, border, border));
            sprite.name = "UiRound" + key;
            _roundSprites[key] = sprite;
            return sprite;
        }

        /// <summary>
        /// 點在不在那張圓角矩形裡（`size` 見方；四角半徑分開給，紋理座標**原點在左下**，
        /// 與 UGUI 的貼圖對應一致 ⇒ 這裡的 `bl／br／tr／tl` 就是 CSS 的
        /// `border-radius: tl tr br bl`）。
        /// </summary>
        private static bool InRounded(float x, float y, float size, float bl, float br, float tr, float tl)
        {
            if (x < 0f || y < 0f || x > size || y > size) return false;
            if (bl > 0f && x < bl && y < bl)
                return (x - bl) * (x - bl) + (y - bl) * (y - bl) <= bl * bl;
            if (br > 0f && x > size - br && y < br)
                return (x - (size - br)) * (x - (size - br)) + (y - br) * (y - br) <= br * br;
            if (tl > 0f && x < tl && y > size - tl)
                return (x - tl) * (x - tl) + (y - (size - tl)) * (y - (size - tl)) <= tl * tl;
            if (tr > 0f && x > size - tr && y > size - tr)
                return (x - (size - tr)) * (x - (size - tr)) + (y - (size - tr)) * (y - (size - tr)) <= tr * tr;
            return true;                                  // 不在任何角落 ⇒ 直邊區域，算在裡面
        }

        // ── 柔和陰影（＝ CSS `box-shadow` 帶**模糊半徑**那一族）──────────────────────
        //
        // 為什麼不能用 UGUI 內建的 `Shadow`：那個是「把同一塊圖位移後再畫一次」＝**硬邊**，
        // 只做得出 `0 3px 0`／`2px 2px 0`（壓條／立體邊）那一種。真相那邊另外還有一整族
        // `0 5px 14px rgba(...)`／`-7px 5px 16px rgba(...)`——那是**真的高斯模糊**：
        // 少了它，浮窗沒有落下感、圓鈕像貼紙（並排圖上看得出來，逐點取色也量得到）。
        //
        // 做法：程序生成一張**模糊過的圓角矩形** sprite，用**九宮格**貼 ⇒ 一張 sprite 套所有尺寸。
        // 為什麼九宮格可行：模糊只改變**邊緣**的剖面，而九宮格的中央是「直的那一段」被拉伸
        // ⇒ 邊緣剖面在拉伸方向上是常數、不會變形（與 `CachedRounded` 同一條思路）。
        //
        // 數學照 Blink（真相那一側的無頭 chromium 就是這條）：CSS 的 blur radius `r`
        // ⇒ 高斯標準差 `σ = r / 2`（`BlurRadiusToStdDev`）。sprite 往外多吃 `pad = ⌈1.5 r⌉`
        // （＝3σ，再遠的尾巴 < 1%，截掉看不出來）。
        //
        // ⚠️ 預設 `SetAsFirstSibling()`（畫在最底下）——這正好是 CSS 的語意：
        //    `box-shadow` 畫在**自己那塊底色的後面**，所以陰影不會把自己的底色弄暗。
        //    它同時會被「後面才建的兄弟」蓋掉，而那也對：`.topbar` 的陰影只有露在
        //    `body` 的**內距那條縫**（y 55～63）與舞台的洞上面。

        /// <summary>模糊陰影向外擴散多少（設計單位）——見上面 σ 的說明。</summary>
        public static int ShadowPad(float blur) => blur <= 0f ? 0 : Mathf.CeilToInt(blur * 1.5f);

        /// <summary>
        /// 掛一顆**柔和陰影**（＝ CSS `box-shadow: dx dy blur color`）。
        ///
        /// ⚠️ **陰影是「目標的兄弟、排在目標前面」**，不是目標的子節點——因為 UGUI 的畫序是
        /// 「父節點自己的圖 → 才輪到子節點」⇒ 掛成子節點的話陰影一定畫在那塊底色**上面**
        /// （把整顆鈕／整張卡弄暗）。排在前面則是 CSS 的語意：`box-shadow` 畫在底色後面。
        ///
        /// ⚠️ **框要在 `Settle` 之後才補**（見 <see cref="SoftShadowSpec"/>）：呼叫當下
        /// `FixedWidth` 那些只是宣告，`LayoutGroup` 還沒排 ⇒ 這裡先建一個**不畫**的佔位節點
        /// ＋一份規格，`Settle` 最後一步照目標結算後的框補上、才開畫。
        ///
        /// `radius` ＝ 被投影那塊的 `border-radius`（圓形傳半徑、直角傳 0）；`blur = 0` 就是
        /// CSS 的實心壓條（`0 3px 0`）。
        /// </summary>
        public static RectTransform ShadowBehind(RectTransform target, string name, Color color,
                                                 float offsetX, float offsetY, float blur,
                                                 float radius = 0f)
        {
            var rect = NewRect(name, target.parent);
            Stretch(rect);
            var image = rect.gameObject.AddComponent<Image>();
            image.color = color;
            image.raycastTarget = false;
            image.sprite = SoftShadowSprite(Mathf.RoundToInt(radius), blur);
            image.type = Image.Type.Sliced;
            image.enabled = false;                 // 位置還沒定 ⇒ 先不畫（免得第一幀糊一片）
            var element = rect.gameObject.AddComponent<LayoutElement>();
            element.ignoreLayout = true;
            var spec = rect.gameObject.AddComponent<SoftShadowSpec>();
            spec.Target = target;
            spec.OffsetX = offsetX;
            spec.OffsetY = offsetY;
            spec.Blur = blur;
            return rect;
        }

        /// <summary>柔和陰影的規格（`Settle` 時照目標的框補位置——見 `ShadowBehind`）。</summary>
        public sealed class SoftShadowSpec : MonoBehaviour
        {
            public RectTransform Target;
            public float OffsetX;
            public float OffsetY;
            public float Blur;
        }

        /// <summary>把每一顆柔和陰影的框照它的目標補上（`Settle` 最後一步叫）。</summary>
        private static void ApplySoftShadows(Transform node)
        {
            var pending = new List<(RectTransform Shadow, SoftShadowSpec Spec)>();
            CollectSoftShadows(node, pending);
            foreach (var (shadow, spec) in pending)
            {
                var target = spec.Target;
                if (target == null) continue;
                var pad = ShadowPad(spec.Blur);
                shadow.anchorMin = target.anchorMin;
                shadow.anchorMax = target.anchorMax;
                shadow.pivot = target.pivot;
                // ⚠️ 用 `offsetMin/offsetMax` 往外吃 `pad`，**不要**用 `sizeDelta += 2×pad`：
                //    後者是「照 pivot 長」——pivot 在上面（`.overlay`／`.topbar` 都是 `pivot.y = 1`）
                //    時整個框只往**下**長，陰影的形狀被推下去一個 `pad`（實測：設定浮窗的陰影
                //    在邊緣就滿格、往下多糊 20px）。`offsetMin/Max` 是相對錨點的角座標，與 pivot 無關。
                shadow.offsetMin = target.offsetMin + new Vector2(-pad + spec.OffsetX, -pad + spec.OffsetY);
                shadow.offsetMax = target.offsetMax + new Vector2(pad + spec.OffsetX, pad + spec.OffsetY);
                shadow.localRotation = target.localRotation;
                shadow.localScale = target.localScale;
                // ⚠️ **要放得進「目標的前一格」而且必須是幂等的**：`Settle` 每屏會被叫**兩次**
                // （`HomeScreen` 建完叫一次、畫之前又叫一次）⇒ 用「直接把目標的 index 給它」
                // 會在第二趟**把陰影推到目標後面**（＝陰影畫在卡片上、整張卡被壓暗）。
                // 這裡改成先看它現在在目標前面還是後面，再決定插哪一格。
                var targetIndex = target.GetSiblingIndex();
                var shadowIndex = shadow.GetSiblingIndex();
                var wanted = shadowIndex < targetIndex ? targetIndex - 1 : targetIndex;
                if (shadowIndex != wanted) shadow.SetSiblingIndex(Mathf.Max(0, wanted));
                var image = shadow.GetComponent<Image>();
                if (image != null) image.enabled = true;
            }
        }

        private static void CollectSoftShadows(Transform node, List<(RectTransform, SoftShadowSpec)> into)
        {
            foreach (Transform child in node)
            {
                if (child is RectTransform rect)
                {
                    var spec = rect.GetComponent<SoftShadowSpec>();
                    if (spec != null && spec.Target != null) into.Add((rect, spec));
                    CollectSoftShadows(child, into);
                }
            }
        }

        // ── 外殼底色（`.app-shell`）＋ **舞台那一格留洞** ─────────────────────────────
        //
        // 為什麼不能是一整片：合成是「UI 畫布在前、立繪從舞台那一格透出來」（見 `NewCanvas`）
        // ⇒ 畫布在**舞台那一格必須是透明的**，否則立繪被蓋掉（實測：主頁補上整片底色之後，
        // 舞台裡的人物整個不見）。但也**不能沒有底色**——沒有底色的話，卡片之間的縫與 body 的
        // 內距露出的是相機的 clear color（spike 留下的 navy-deep ⇒ 整片深藍，
        // 實測 Unity (24,60,102) vs 真相 (253,254,255)）。
        //
        // 做法：**圍著舞台的四條**（上／下／左／右）。四條共用**同一張「畫布大小」的漸層貼圖**、
        // 各用 `RawImage.uvRect` 取自己那一段 ⇒ 接起來就是原本那片連續的 165° 漸層
        // （不是四段各自壓縮——那樣每條中間都會比真相暗一階）。
        // 舞台的框要等 grid 排完才知道 ⇒ 這件事放在 `Settle`（見 `ApplyShellHoles`）。

        /// <summary>
        /// `.app-shell { background: linear-gradient(165deg, #f6fbff 0%, #eaf6ff 55%, #f7fbff 100%) }`
        /// ——**舞台那一格留洞**的版本（`hole` 傳舞台的 `RectTransform`）。
        /// </summary>
        public static RectTransform ShellBackdrop(RectTransform shell, RectTransform hole, float degrees,
                                                  float[] at, Color[] cols)
        {
            var spec = shell.GetComponent<ShellHoleSpec>() ?? shell.gameObject.AddComponent<ShellHoleSpec>();
            spec.Hole = hole;
            spec.Degrees = degrees;
            spec.At = at;
            spec.Cols = cols;
            return shell;
        }

        /// <summary>外殼底色留洞的規格（`Settle` 時按舞台結算後的框擺四條——見 `ShellBackdrop`）。</summary>
        public sealed class ShellHoleSpec : MonoBehaviour
        {
            public RectTransform Hole;
            public float Degrees;
            public float[] At = System.Array.Empty<float>();
            public Color[] Cols = System.Array.Empty<Color>();
            public RectTransform[] Strips;
            public RectTransform Ring;
            public RectTransform Dim;
            public Color? DimColor;
        }

        /// <summary>
        /// `.overlay-active .app-shell::after { background: rgba(23,47,74,.38) }`——開浮窗時整片
        /// 壓一層暗（畫在**外殼底色之上、浮窗之下**；設定頁那一片）。
        /// ⚠️ 這一層先前**完全沒畫**：整片底比真相亮一階多（實測設定頁背景 57.9% 的像素都差）。
        /// </summary>
        public static void ShellDim(RectTransform shell, Color color)
        {
            var spec = shell.GetComponent<ShellHoleSpec>() ?? shell.gameObject.AddComponent<ShellHoleSpec>();
            spec.DimColor = color;
        }

        /// <summary>擺四條（上／下／左／右）＋把每一條的 `uvRect` 指到它在整片漸層裡的那一段。</summary>
        private static void ApplyShellHoles(Transform node)
        {
            var specs = new List<(RectTransform Shell, ShellHoleSpec Spec)>();
            CollectShellHoles(node, specs);
            foreach (var (shell, spec) in specs)
            {
                if (spec.At.Length < 2 || spec.Cols.Length != spec.At.Length) continue;
                var width = Mathf.Max(1, Mathf.RoundToInt(shell.rect.width));
                var height = Mathf.Max(1, Mathf.RoundToInt(shell.rect.height));
                var list = new (float, Color)[spec.At.Length];
                for (var i = 0; i < list.Length; i++) list[i] = (spec.At[i], spec.Cols[i]);
                var texture = DiagonalGradientStops(width, height, spec.Degrees, list);   // 快取過
                // ⚠️ `Hole == null` ＝**不留洞**（沒有舞台的屏：選單／設定…）⇒ 一條鋪滿整片畫布。
                var hole = spec.Hole != null ? DesignRect(shell, spec.Hole)              // x, y(往下), 寬, 高
                                             : new Vector4(0f, 0f, 0f, 0f);
                var bands = spec.Hole == null
                    ? new[] { new Vector4(0f, 0f, width, height) }
                    : new[]
                    {
                        new Vector4(0f, 0f, width, hole.y),                                        // 上
                        new Vector4(0f, hole.y + hole.w, width, height - hole.y - hole.w),         // 下
                        new Vector4(0f, hole.y, hole.x, hole.w),                                   // 左
                        new Vector4(hole.x + hole.z, hole.y, width - hole.x - hole.z, hole.w),     // 右
                    };
                if (spec.Strips == null || spec.Strips.Length != bands.Length)
                {
                    spec.Strips = new RectTransform[bands.Length];
                    for (var i = 0; i < bands.Length; i++)
                    {
                        var strip = NewRect("ShellBand" + i, shell);
                        strip.gameObject.AddComponent<LayoutElement>().ignoreLayout = true;
                        var raw = strip.gameObject.AddComponent<RawImage>();
                        raw.raycastTarget = false;
                        strip.SetAsFirstSibling();       // 一定在內容之前（各條互不重疊 ⇒ 彼此順序無所謂）
                        spec.Strips[i] = strip;
                    }
                }
                for (var i = 0; i < bands.Length; i++)
                {
                    var band = bands[i];
                    var strip = spec.Strips[i];
                    var raw = strip.GetComponent<RawImage>();
                    if (raw == null) continue;
                    raw.texture = texture;
                    raw.uvRect = new Rect(band.x / width, 1f - (band.y + band.w) / height,
                                          band.z / width, band.w / height);
                    strip.anchorMin = strip.anchorMax = new Vector2(0f, 1f);
                    strip.pivot = new Vector2(0f, 1f);
                    strip.anchoredPosition = new Vector2(band.x, -band.y);
                    strip.sizeDelta = new Vector2(Mathf.Max(0f, band.z), Mathf.Max(0f, band.w));
                }
                // `.app-shell { box-shadow: inset 0 0 0 3px rgba(255,255,255,.8) }`——最外圈那 3px 白。
                // 它畫在**底色之上、內容之下**（CSS 的 inset shadow 就是這樣）⇒ 排在各條之後、
                // 其他一切之前。先前整個沒畫（真相最外 3px 是近白 (253,254,255)、我們是底色 (242,249,255)）。
                if (spec.Ring == null)
                {
                    spec.Ring = NewRect("ShellRing", shell);
                    spec.Ring.gameObject.AddComponent<LayoutElement>().ignoreLayout = true;
                    var ringImage = spec.Ring.gameObject.AddComponent<Image>();
                    ringImage.raycastTarget = false;
                    ringImage.sprite = RingSprite(Mathf.RoundToInt(DesignTokens.ShellCorner),
                                                  Mathf.RoundToInt(DesignTokens.ShellRingWidth));
                    ringImage.type = Image.Type.Sliced;
                    ringImage.color = DesignTokens.ShellRing;
                }
                Stretch(spec.Ring);
                // ⚠️ 白環（`inset 0 0 0 3px`）落在**外框之內**：不內縮就會蓋掉那條 1px 外框。
                var inset = DesignTokens.ShellEdgeWidth;
                spec.Ring.offsetMin = new Vector2(inset, inset);
                spec.Ring.offsetMax = new Vector2(-inset, -inset);
                spec.Ring.SetSiblingIndex(Mathf.Min(bands.Length, shell.childCount - 1));
                // `.overlay-active .app-shell::after`（開浮窗時整片壓暗的那一層）——排在環之後、
                // 浮窗之前（CSS：`::after` z-index 6 ＜ `.overlay` 的 7）。
                if (spec.DimColor.HasValue)
                {
                    if (spec.Dim == null)
                    {
                        spec.Dim = NewRect("ShellDim", shell);
                        spec.Dim.gameObject.AddComponent<LayoutElement>().ignoreLayout = true;
                        var dimImage = spec.Dim.gameObject.AddComponent<Image>();
                        dimImage.raycastTarget = false;
                        dimImage.color = spec.DimColor.Value;
                    }
                    Stretch(spec.Dim);
                    // CSS 的 `inset: 0` 是**padding box** ⇒ 壓暗層也在 1px 外框之內。
                    spec.Dim.offsetMin = new Vector2(DesignTokens.ShellEdgeWidth, DesignTokens.ShellEdgeWidth);
                    spec.Dim.offsetMax = new Vector2(-DesignTokens.ShellEdgeWidth, -DesignTokens.ShellEdgeWidth);
                    spec.Dim.SetSiblingIndex(Mathf.Min(bands.Length + 1, shell.childCount - 1));
                }
            }
        }

        private static void CollectShellHoles(Transform node, List<(RectTransform, ShellHoleSpec)> into)
        {
            // ⚠️ **規格是掛在畫布根節點身上的**（`ShellBackdrop(root, …)`）⇒ 要先看 `node` 自己，
            //    只看子節點會一條都找不到（實測：主頁底色整個沒鋪、畫面退回相機的 navy-deep）。
            if (node is RectTransform self)
            {
                var own = self.GetComponent<ShellHoleSpec>();
                if (own != null) into.Add((self, own));
            }
            foreach (Transform child in node)
            {
                if (child is RectTransform rect) CollectShellHoles(rect, into);
            }
        }

        private static readonly Dictionary<int, Sprite> _shadowSprites = new Dictionary<int, Sprite>();

        /// <summary>
        /// 一張**模糊過的圓角矩形**（白；實際顏色走 `Image.color`，alpha＝覆蓋率再模糊）。
        ///
        /// ⚠️ **尺寸推導（這裡錯了整套模糊陰影等於沒畫，實測踩過）**：九宮格把**中央那一段拉伸**，
        /// 所以「被拉伸的那一段」必須落在**模糊之後仍然是 alpha 1 的內核**裡。高斯核半徑
        /// `k = 3σ`（`σ = blur/2`）⇒ 內核＝形狀往內縮 `k` 之後那塊 ⇒
        /// 角格 `B = radius + 2k`、`size = 2B + 2`、形狀盒 `= size − 2k`。
        /// 先前寫成 `B = radius + k`、形狀盒只有 2px 寬 ⇒ 那 2px 的質量被正規化的高斯攤掉
        /// （峰值只剩 ~2%），**畫出來幾乎全透明**（模糊的陰影全部看不到，只有 blur=0 的實心壓條正常）。
        /// </summary>
        private static Sprite SoftShadowSprite(int radius, float blur)
        {
            var pad = ShadowPad(blur);
            var half = Mathf.Max(1, radius + 2 * pad);
            var key = half * 1000 + radius;
            if (_shadowSprites.TryGetValue(key, out var cached)) return cached;

            var size = half * 2 + 2;
            var box = size - 2 * pad;                          // 被投影那塊的邊長
            // ① 覆蓋率：圓角矩形，4× 子像素超取樣（邊緣才不會有階梯）。
            const int samples = 4;
            var coverage = new float[size * size];
            for (var y = 0; y < size; y++)
            {
                for (var x = 0; x < size; x++)
                {
                    var hit = 0;
                    for (var sy = 0; sy < samples; sy++)
                    {
                        for (var sx = 0; sx < samples; sx++)
                        {
                            var px = x + (sx + 0.5f) / samples - pad;
                            var py = y + (sy + 0.5f) / samples - pad;
                            if (InRounded(px, py, box, radius, radius, radius, radius)) hit++;
                        }
                    }
                    coverage[y * size + x] = (float)hit / (samples * samples);
                }
            }
            // ② 可分離高斯（σ = blur/2，核半徑 3σ）。邊界外當 0——尾巴已被 `pad` 包住。
            if (blur > 0f)
            {
                var sigma = blur * 0.5f;
                var reach = Mathf.Max(1, Mathf.CeilToInt(sigma * 3f));
                var kernel = new float[reach + 1];
                var sum = 0f;
                for (var i = 0; i <= reach; i++)
                {
                    kernel[i] = Mathf.Exp(-(i * i) / (2f * sigma * sigma));
                    sum += i == 0 ? kernel[0] : 2f * kernel[i];
                }
                for (var i = 0; i <= reach; i++) kernel[i] /= sum;
                coverage = BlurAxis(coverage, size, kernel, true);
                coverage = BlurAxis(coverage, size, kernel, false);
            }
            var pixels = new Color32[size * size];
            for (var i = 0; i < pixels.Length; i++)
                pixels[i] = new Color32(255, 255, 255,
                                        (byte)Mathf.RoundToInt(255f * Mathf.Clamp01(coverage[i])));
            var texture = new Texture2D(size, size, TextureFormat.RGBA32, false);
            texture.SetPixels32(pixels);
            texture.Apply();
            var border = Mathf.Max(1, half);
            var sprite = Sprite.Create(texture, new Rect(0f, 0f, size, size), new Vector2(0.5f, 0.5f), 100f, 0,
                                       SpriteMeshType.FullRect,
                                       new Vector4(border, border, border, border));
            sprite.name = "UiShadow" + key;
            _shadowSprites[key] = sprite;
            return sprite;
        }

        /// <summary>沿一軸做一次 1 維卷積（`horizontal` 決定掃哪一軸；外框外當 0）。</summary>
        private static float[] BlurAxis(float[] src, int size, float[] kernel, bool horizontal)
        {
            var dst = new float[src.Length];
            var reach = kernel.Length - 1;
            for (var o = 0; o < size; o++)
            {
                for (var i = 0; i < size; i++)
                {
                    var acc = 0f;
                    for (var t = -reach; t <= reach; t++)
                    {
                        var j = i + t;
                        if (j < 0 || j >= size) continue;
                        acc += (horizontal ? src[o * size + j] : src[j * size + o]) * kernel[Mathf.Abs(t)];
                    }
                    if (horizontal) dst[o * size + i] = acc;
                    else dst[i * size + o] = acc;
                }
            }
            return dst;
        }

        private static Sprite _circleSprite;

        /// <summary>
        /// 對話氣泡的頭像（`Resources/avatars/<角色 id>.png`）——那些 PNG 是**烤好的**：
        /// 白底 ＋ `cover` 置中裁切 ＋ **圓形 alpha**（`tools/sync_avatars.sh`，對應真相的
        /// `background: #fff var(--spk-avatar) center/cover` ＋ `border-radius: 50%`）
        /// ⇒ 執行期只要一個 `RawImage`，不必在 Unity 裡疊 `Mask`。
        ///
        /// 查不到那個角色就退回 `_fallback.png`（＝真相 `url('../assets/aster_icon_normal.png')`
        /// 那條退路：舊紀錄沒有角色欄位）；兩個都沒有才回 null（那一格不畫，
        /// 免得它變成一塊白方塊騙過眼睛——文字 dump 反正看不到圖）。
        /// </summary>
        public static Texture2D AvatarTexture(string id)
        {
            if (_avatars == null)
            {
                _avatars = new Dictionary<string, Texture2D>();
                foreach (var texture in Resources.LoadAll<Texture2D>("avatars"))
                    _avatars[texture.name] = texture;
                Debug.Log($"[screen] 頭像載入 {_avatars.Count} 張（Resources/avatars）");
            }
            if (!string.IsNullOrEmpty(id) && _avatars.TryGetValue(id, out var found)) return found;
            return _avatars.TryGetValue("_fallback", out var fallback) ? fallback : null;
        }

        private static Dictionary<string, Texture2D> _avatars;

        /// <summary>
        /// **設定頁卡片縮圖**：照投影的 `image` 路徑取**已經烤好**的貼圖。
        ///
        /// * `characters/<id>/…` ⇒ 走頭像那一桶（`AvatarTexture(id)`；角色卡的縮圖就是那張立繪）；
        /// * `backgrounds/<id>/thumb.jpg` ⇒ `Resources/backgrounds/<id>`（`tools/sync_thumbs.sh` 烤的
        ///   288×168 ＝ 設計 72×42 的 4 倍，白底 ＋ `object-fit: cover` 置中裁切）；
        /// * 空字串（「無背景」那張卡**沒有檔案**）⇒ `Resources/backgrounds/_plain`（照 CSS 畫的斜線紋）。
        ///
        /// 為什麼讀烤好的、不在執行期裁：`cover` 的置中裁切在 `RawImage` 上要自己算 `uvRect`，
        /// 而這件事烤一次就固定（與頭像同一條紀律，見 `sync_avatars.sh`）。
        /// 查不到就不畫那一格（回 null）——免得變成一塊白方塊騙過眼睛。
        /// </summary>
        public static Texture2D CardThumbTexture(string imagePath, string kind)
        {
            if (string.IsNullOrEmpty(imagePath))
                return kind == "background" ? PackedTexture("backgrounds", "_plain") : null;
            var parts = imagePath.Split('/');
            if (parts.Length < 3) return null;
            if (parts[0] == "characters") return AvatarTexture(parts[1]);
            if (parts[0] != "backgrounds") return null;
            // `thumb.jpg` 是那一目錄的主縮圖 ⇒ 名字用目錄名；其他（`dawn_thumb.jpg` 之類）加前置目錄名。
            var file = parts[2];
            var name = file == "thumb.jpg"
                ? parts[1]
                : parts[1] + "_" + file.Replace("_thumb.jpg", "").Replace(".jpg", "");
            return PackedTexture("backgrounds", name);
        }

        private static readonly Dictionary<string, Dictionary<string, Texture2D>> _packed = new();

        /// <summary>`Resources/&lt;folder&gt;/&lt;name&gt;` 的貼圖（按資料夾快取；查不到回 null）。</summary>
        public static Texture2D PackedTexture(string folder, string name)
        {
            if (!_packed.TryGetValue(folder, out var bucket))
            {
                bucket = new Dictionary<string, Texture2D>();
                foreach (var texture in Resources.LoadAll<Texture2D>(folder)) bucket[texture.name] = texture;
                _packed[folder] = bucket;
                Debug.Log($"[screen] 貼圖載入 {bucket.Count} 張（Resources/{folder}）");
            }
            return bucket.TryGetValue(name, out var found) ? found : null;
        }

        /// <summary>
        /// **收縮到內容寬**（＝ CSS 在 flex 容器裡 `width: auto` 那個行為）。
        ///
        /// 為什麼要它：**`VerticalLayoutGroup`／`HorizontalLayoutGroup` 自己會回報 `flexibleWidth = 1`**
        /// （`LayoutUtility` 取的是所有 `ILayoutElement` 的**最大**值）⇒ 只要父容器是 `Row`，
        /// 那個區塊就會被**撐滿整列**。實測（對話氣泡那一屏）：`max-width: 84%` 的訊息列被撐成
        /// 430（＝整條串的內框寬），而真相是**收縮到內容寬**（他那一則 100、她那一則 126）。
        /// 釘住 `flexibleWidth = 0` 之後，父容器才會照 preferred 給 ⇒ 與 CSS 的 shrink-to-fit 同義。
        /// </summary>
        public static LayoutElement ShrinkToFit(RectTransform rect)
        {
            var element = rect.GetComponent<LayoutElement>() ?? rect.gameObject.AddComponent<LayoutElement>();
            element.flexibleWidth = 0f;
            element.flexibleHeight = 0f;
            return element;
        }

        /// <summary>固定高度（對照原生的 `dp` 值）。weight 為 0 ⇒ 不參與分配。</summary>
        public static LayoutElement FixedHeight(RectTransform rect, float height)
        {
            var element = rect.GetComponent<LayoutElement>() ?? rect.gameObject.AddComponent<LayoutElement>();
            element.minHeight = height;
            element.preferredHeight = height;
            element.flexibleHeight = 0f;
            return element;
        }

        /// <summary>固定寬度。</summary>
        public static LayoutElement FixedWidth(RectTransform rect, float width)
        {
            var element = rect.GetComponent<LayoutElement>() ?? rect.gameObject.AddComponent<LayoutElement>();
            element.minWidth = width;
            element.preferredWidth = width;
            element.flexibleWidth = 0f;
            return element;
        }

        /// <summary>
        /// **一格 CSS grid 的列**：給 <see cref="PinGridRows"/> 用。`Flex` ＝ `minmax(min, fr)`、
        /// `Fixed` ＝ 不彈性的一列（`auto` 列＝先量到的自然高、或寫死值）。
        /// </summary>
        public readonly struct GridRow
        {
            public readonly RectTransform Rect;
            public readonly float Min;
            public readonly float Fr;
            public readonly float Height;

            private GridRow(RectTransform rect, float min, float fr, float height)
            {
                Rect = rect; Min = min; Fr = fr; Height = height;
            }

            /// <summary>`minmax(min, fr fr)`：這一列**至少 min**，其餘照 fr 分（見 `PinGridRows`）。</summary>
            public static GridRow Flex(RectTransform rect, float min, float fr) => new GridRow(rect, min, fr, 0f);

            /// <summary>不參與 fr 的一列：`auto` 列給**量到的自然高**、寫死的列給常數。</summary>
            public static GridRow Fixed(RectTransform rect, float height) => new GridRow(rect, 0f, 0f, height);
        }

        /// <summary>
        /// **照 CSS grid 的規則把每一列的高度算出來釘死**（＝ skill §「UGUI 加權列」那條的正解）。
        ///
        /// 為什麼不能只靠 `Weight`：`VerticalLayoutGroup` 分配多餘高度時，子節點的**最小高度是整棵
        /// 子樹算出來的 min**（`LayoutElement.minHeight` 只是其中一個來源、壓不下去）⇒ 內容多的那一列
        /// 縮不到 `Nfr` 該有的高度，多出來的空間再從別的 `fr` 列扣走。實測（文字 A/B 全綠、只有座標／
        /// 像素看得出來）：home 舞台 343 vs 真相 384、對話卡 244 vs 199；game 棋盤那一列差 25。
        ///
        /// 演算法照 CSS Grid §12.7「Find the Size of an fr」——**注意分子只扣非彈性列**：
        /// <code>
        ///   fr ＝ (可分配高度 − Σ非彈性列高) / Σfr      ⇒ 每個彈性列高 ＝ max(它的 min, fr × 自己的 fr)
        /// </code>
        /// 第一版照「(全部 − 所有列的 min)/Σfr」算，home 差 1.6px、game 差 25px（棋盤）。
        /// 被自己的 min 頂住的列要**當成非彈性列再算一次**（規格裡的 restart）——本專案棋盤那一列
        /// 就是靠這條落到 352（＝真相）。
        ///
        /// ⚠️ 呼叫時機：容器本身的高度要**已經結算過**（`Settle`），而且 `auto` 列要先量到自然高。
        /// ⚠️ `pageFrameInset`：真相那一頁有 1px 外框，內容區比同尺寸的 Unity 畫布**少 2px**
        ///   （見 `DesignTokens.PageFrameInset`）⇒ 不扣的話每一列各差 1～2px。
        /// </summary>
        public static void PinGridRows(RectTransform container, float padTop, float padBottom, float gap,
                                       float pageFrameInset, params GridRow[] rows)
            => PinGridRowsCore(container, 0f, -1, padTop, padBottom, gap, pageFrameInset, rows);

        /// <summary>
        /// 同 <see cref="PinGridRows"/>，但**比設計格高出來的那一段**（<paramref name="extra"/>，長螢幕才有）
        /// 不照 fr 分，**整段給第 <paramref name="growIndex"/> 列**（owner 2026-10-18：長手機多出來的高度給舞台，
        /// 不要撐大下面那張幾乎空著的對話卡）。其餘各列＝設計格高度下照 CSS grid 算出來的那個值。
        /// </summary>
        public static void PinGridRowsGrow(RectTransform container, float extra, int growIndex,
                                           float padTop, float padBottom, float gap,
                                           float pageFrameInset, params GridRow[] rows)
            => PinGridRowsCore(container, Mathf.Max(0f, extra), growIndex, padTop, padBottom, gap, pageFrameInset, rows);

        private static void PinGridRowsCore(RectTransform container, float extra, int growIndex,
                                            float padTop, float padBottom, float gap,
                                            float pageFrameInset, GridRow[] rows)
        {
            if (growIndex < 0 || growIndex >= rows.Length) extra = 0f;
            var space = container.rect.height - extra - padTop - padBottom - gap * (rows.Length - 1)
                        - pageFrameInset * 2f;
            var heights = new float[rows.Length];
            for (var i = 0; i < rows.Length; i++)
            {
                if (rows[i].Fr > 0f) continue;
                heights[i] = rows[i].Height;
                space -= rows[i].Height;
            }
            // 被 min 頂住的列 ⇒ 之後當成固定列（規格：restart）。最多跑 rows.Length 輪（每輪至少釘一列）。
            var pinned = new bool[rows.Length];
            for (var round = 0; round <= rows.Length; round++)
            {
                var divisor = 0f;
                for (var i = 0; i < rows.Length; i++)
                    if (rows[i].Fr > 0f && !pinned[i]) divisor += rows[i].Fr;
                if (divisor <= 0f) break;
                if (divisor < 1f) divisor = 1f;       // CSS：Σfr < 1 ⇒ 當 1（用不到的空間不分配）
                var fr = space / divisor;
                var restart = false;
                for (var i = 0; i < rows.Length; i++)
                {
                    if (rows[i].Fr <= 0f || pinned[i]) continue;
                    if (rows[i].Fr * fr < rows[i].Min)
                    {
                        pinned[i] = true;
                        heights[i] = rows[i].Min;
                        space -= rows[i].Min;
                        restart = true;
                    }
                }
                if (restart) continue;
                for (var i = 0; i < rows.Length; i++)
                    if (rows[i].Fr > 0f && !pinned[i]) heights[i] = rows[i].Fr * fr;
                break;
            }
            if (extra > 0f) heights[growIndex] += extra;
            for (var i = 0; i < rows.Length; i++) FixedHeight(rows[i].Rect, heights[i]);
        }

        /// <summary>
        /// **一個文字節點該有多高＝整數行**（依它**現在的寬度**算 preferred，再四捨五入到 `lineHeight`
        /// 的整數倍）。
        ///
        /// 為什麼要它：真相的 `<p>` 是 CSS 行盒（`.question-card p`：12px／`line-height: normal` ⇒ 16px
        /// 一行），**句子長了會折行**——實測 `#hand-sheet` 的日文那一句是 2 行（`<p>` 高 32、整張卡 165）。
        /// 寫死一個行高會把第二行裁掉，而**文字 dump 照樣全綠**（dump 讀欄位、不看有沒有畫出來）——
        /// 這與「`SelectTextBackend` 漏呼叫」是同一類的靜默缺陷。
        ///
        /// ⚠️ **要在 `Settle` 之後呼叫**（寬度還沒結算時 preferred 是拿舊寬算的），改完再 `Settle` 一次。
        /// </summary>
        public static float WrappedHeight(Component text, float lineHeight)
        {
            float preferred;
            if (text is TMPro.TMP_Text tmp) preferred = tmp.preferredHeight;
            else if (text is Text legacy) preferred = legacy.preferredHeight;
            else return lineHeight;
            var lines = Mathf.Max(1, Mathf.RoundToInt(preferred / lineHeight));
            return lines * lineHeight;
        }

        /// <summary>
        /// **可以打字的輸入格**：真相的 `#history-input` 是 `contenteditable="true"` 的 div
        /// （`.chat-composer .chat-input`：31 高、radius 16、1px `#cbd9df`、白底）——
        /// Unity 這邊沒有 contenteditable ⇒ 用 `TMP_InputField` 套在同一個外觀上。
        ///
        /// ⚠️ **三個一定要接的東西**（少一個就「看得到框、打不進字」）：`textViewport`
        /// （帶 `RectMask2D` 的父節點，超出框的文字要裁掉）、`textComponent`、`placeholder`。
        /// ⚠️ **游標**＝`shouldHideMobileInput = true`（見函式裡的說明）：手機上不藏系統那條輸入列，
        /// TMP 就不畫游標（設計如此）。系統輸入法照樣叫得出來。
        /// ⚠️ `richText = false`：訊息框不該把使用者打的 `<` 當標籤。
        /// </summary>
        public static TMP_InputField Input(string node, Transform parent, string placeholder,
                                           float fontSize, Color textColor, Color placeholderColor,
                                           float height, float radius, float padX)
        {
            var rect = NewRect(node, parent);
            // ⚠️ **游標為什麼看不到**（owner 2026-10-30 兩次回報）＋ 社群公認解法：
            //   `TMP_InputField` 的 caret 物件是在 `OnEnable()` 裡建的，且要求當時
            //   `m_TextComponent` 已經指派好。我們的欄位是「先 AddComponent、後指派」
            //   ⇒ OnEnable 當下 textComponent 還是 null，caret 從此沒被建出來
            //   （能打字、只是永遠沒有游標）。
            //   解法：**先把整個物件停用**，把子物件與所有指派都做完，最後才 SetActive(true)
            //   —— 這樣 Awake/OnEnable 只會「一切就緒」地跑一次。
            rect.gameObject.SetActive(false);
            var bg = rect.gameObject.AddComponent<Image>();
            bg.color = Color.white;
            Round(bg, radius);
            FixedHeight(rect, height);

            var field = rect.gameObject.AddComponent<TMP_InputField>();

            var viewport = NewRect(node + "Viewport", (Transform)rect);
            Stretch(viewport);
            viewport.offsetMin = new Vector2(padX, 0f);
            viewport.offsetMax = new Vector2(-padX, 0f);
            viewport.gameObject.AddComponent<RectMask2D>();

            var text = Text(node + "Text", viewport, "", fontSize, textColor,
                            TextAlignmentOptions.MidlineLeft, noWrap: true);
            Stretch((RectTransform)text.transform);
            var placeholderText = Text(node + "Placeholder", viewport, placeholder, fontSize, placeholderColor,
                                       TextAlignmentOptions.MidlineLeft, noWrap: true);
            Stretch((RectTransform)placeholderText.transform);

            field.textViewport = viewport;
            field.textComponent = (TMP_Text)text;
            field.placeholder = (Graphic)placeholderText;
            field.targetGraphic = bg;
            field.lineType = TMP_InputField.LineType.SingleLine;
            // ⚠️ `customCaretColor` 沒開的話 `caretColor` 是**不生效**的（TMP 只有在開啟時才用這個顏色）
            field.customCaretColor = true;
            field.caretColor = textColor;
            field.selectionColor = new Color(textColor.r, textColor.g, textColor.b, 0.3f);
            field.richText = false;
            // 到這裡為止一切都設定好了（textComponent／viewport／placeholder／顏色），
            // 下面才把整個物件啟用 ⇒ Awake／OnEnable 在「欄位齊全」的狀態下只跑一次，
            // caret 就是那時被正確建出來的（見函式開頭的說明）。
            // 寬 2：預設 1 在高 DPI 上太細。
            field.caretWidth = 2;
            // ⚠️ **游標真正的開關**（2026-10-30 探針定案）：
            //   手機上 TMP 只有「就地編輯」時才畫游標——`UpdateGeometry()` 開頭
            //   `if (InPlaceEditing() == false) return;`，而不藏系統輸入列（`shouldHideMobileInput`
            //   預設 false）＝不就地編輯 ⇒ caret 物件、閃爍旗標都在，就是**從不出網格**（當時探針正是這樣）。
            //   藏起來 ⇒ 就地編輯：畫游標、點字移游標；系統鍵盤與輸入法照常（iOS／Android 都把選取同步回來）。
            //   Mac 播放器、iOS 模擬器實測：網格 4 頂點、亮暗交替，截圖看得到。
            field.shouldHideMobileInput = true;
            // 這裡才啟用：Awake／OnEnable 會在「textComponent、viewport、placeholder 都到位」
            // 的狀態下跑一次，caret 就是那時候被正確建出來的。
            rect.gameObject.SetActive(true);
            field.ForceLabelUpdate();
            return field;
        }

        /// <summary>一塊底色（半透明面板、chip 那種）。`radius > 0` ⇒ 圓角（見 `Round`）。</summary>
        /// <summary>
        /// **徑向漸層的底**：CSS `radial-gradient(circle at X% Y%, <內色>, <外色> N%)` 的 Unity 對應物。
        ///
        /// 為什麼要程序生成：UGUI 的 `Image` 只會平塗或吃一張 sprite ⇒ 漸層一定要有一張貼圖；
        /// 而這一張是**算出來的**（與 `RingSprite` 同一個手法：不引任何資產，`Library/` 重建也不會掉）。
        ///
        /// CSS 的 `circle`（不給長度時）是 **farthest-corner**：色停點 `N%` 指的是「圓心到最遠角的
        /// 距離的 N%」。棋盤那一塊就是這個：`radial-gradient(circle at 50% 42%, #eff8fe, #d6ecfa 78%)`。
        /// </summary>
        public static RectTransform RadialFill(string node, Transform parent, Color inner, Color outer,
                                               float centreX, float centreY, float stop)
        {
            var rect = NewRect(node, parent);
            var image = rect.gameObject.AddComponent<Image>();
            image.sprite = RadialSprite(inner, outer, centreX, centreY, stop);
            image.type = Image.Type.Simple;
            image.raycastTarget = false;
            return rect;
        }

        private static Sprite _radialSprite;
        private static string _radialKey;

        private static Sprite RadialSprite(Color inner, Color outer, float centreX, float centreY, float stop)
        {
            var key = $"{inner}:{outer}:{centreX}:{centreY}:{stop}";
            if (_radialSprite != null && _radialKey == key) return _radialSprite;

            const int size = 256;               // 棋盤那一格約 330 px ⇒ 256 夠（再大只是浪費）
            var texture = new Texture2D(size, size, TextureFormat.RGBA32, false)
            {
                wrapMode = TextureWrapMode.Clamp,
                filterMode = FilterMode.Bilinear,
            };
            var cx = centreX * size;
            var cy = (1f - centreY) * size;     // CSS 的 y 由上往下；貼圖的列由上往下 ⇒ 直接對上
            var farthest = 0f;
            foreach (var (px, py) in new[] { (0f, 0f), (size, 0f), (0f, size), ((float)size, (float)size) })
            {
                var d = Mathf.Sqrt((px - cx) * (px - cx) + (py - cy) * (py - cy));
                if (d > farthest) farthest = d;
            }
            if (farthest <= 0f) farthest = 1f;

            var pixels = new Color[size * size];
            for (var y = 0; y < size; y++)
            {
                for (var x = 0; x < size; x++)
                {
                    var d = Mathf.Sqrt((x - cx) * (x - cx) + (y - cy) * (y - cy)) / farthest;
                    var t = Mathf.Clamp01(stop <= 0f ? 1f : d / stop);
                    pixels[y * size + x] = Color.Lerp(inner, outer, t);
                }
            }
            texture.SetPixels(pixels);
            texture.Apply();

            _radialSprite = Sprite.Create(texture, new Rect(0f, 0f, size, size), new Vector2(0.5f, 0.5f),
                                          100f, 0, SpriteMeshType.FullRect);
            _radialKey = key;
            return _radialSprite;
        }

        /// <summary>
        /// **斜紋裝飾**（`.topbar::before`：左邊 88px、`opacity: .3`、兩個 ±60° 的帶狀漸層疊起來）。
        ///
        /// 為什麼要程序生成貼圖：UGUI 沒有「斜向帶狀漸層」這種東西（`GradientStopsFill` 只做水平
        /// 或 180°）。這裡把 CSS 那兩個 `linear-gradient` 逐像素算出來：`t` ＝「點投影到漸層方向
        /// 線上的比例」（0…1，CSS 的定義：方向線長度 ＝ |w·sin a| ＋ |h·cos a|），再照色停點疊。
        /// </summary>
        public static RectTransform StripeFill(string node, Transform parent, float width, float height,
                                               Color stripeA, float a0, float a1,
                                               Color stripeB, float b0, float b1, float opacity)
        {
            var rect = NewRect(node, parent);
            var image = rect.gameObject.AddComponent<Image>();
            image.sprite = StripeSprite(width, height, stripeA, a0, a1, stripeB, b0, b1, opacity);
            image.type = Image.Type.Simple;
            image.raycastTarget = false;
            return rect;
        }

        private static Sprite _stripeSprite;
        private static string _stripeKey;

        private static Sprite StripeSprite(float width, float height, Color a, float a0, float a1,
                                           Color b, float b0, float b1, float opacity)
        {
            var key = $"{width}:{height}:{a}:{a0}:{a1}:{b}:{b0}:{b1}:{opacity}";
            if (_stripeSprite != null && _stripeKey == key) return _stripeSprite;

            var sk = Mathf.Max(2, TexelScale);                     // 照螢幕密度提高解析度（斜邊才不會有鋸齒帶）
            var w = Mathf.Max(2, Mathf.RoundToInt(width * sk));
            var h = Mathf.Max(2, Mathf.RoundToInt(height * sk));
            var texture = new Texture2D(w, h, TextureFormat.RGBA32, false)
            {
                wrapMode = TextureWrapMode.Clamp,
                filterMode = FilterMode.Bilinear,
            };
            var pixels = new Color[w * h];
            for (var y = 0; y < h; y++)
            {
                for (var x = 0; x < w; x++)
                {
                    // 座標換回「CSS 的框」（貼圖是 ×sk 的）
                    var px = (x + 0.5f) / w * width;
                    var py = (y + 0.5f) / h * height;
                    var under = Band(px, py, width, height, 60f, a0, a1, a);      // 下面那一層
                    var over = Band(px, py, width, height, -60f, b0, b1, b);      // 上面那一層
                    var mixed = Color.Lerp(under, over, over.a);                  // 上面蓋下面（alpha 混合）
                    mixed.a = over.a + under.a * (1f - over.a);
                    mixed.a *= opacity;                                           // `.topbar::before { opacity: .3 }`
                    pixels[y * w + x] = mixed;
                }
            }
            texture.SetPixels(pixels);
            texture.Apply();

            _stripeSprite = Sprite.Create(texture, new Rect(0f, 0f, w, h), new Vector2(0.5f, 0.5f),
                                          100f, 0, SpriteMeshType.FullRect);
            _stripeKey = key;
            return _stripeSprite;
        }

        /// <summary>一個 `linear-gradient(<角度>, transparent <x>%, <色> <x>% <y>%, transparent <y>%)`
        /// 在那個點上的顏色（色帶內＝全不透明、色帶外＝透明）。</summary>
        private static Color Band(float px, float py, float width, float height, float angleDeg,
                                  float from, float to, Color color)
        {
            var radians = angleDeg * Mathf.Deg2Rad;
            var dirX = Mathf.Sin(radians);
            var dirY = -Mathf.Cos(radians);                       // CSS：0° 朝上、順時針 ⇒ 螢幕 y 向下取負
            var length = Mathf.Abs(width * dirX) + Mathf.Abs(height * dirY);
            if (length <= 0f) return new Color(0f, 0f, 0f, 0f);
            var t = 0.5f + ((px - width / 2f) * dirX + (py - height / 2f) * dirY) / length;
            return (t >= from && t <= to) ? color : new Color(0f, 0f, 0f, 0f);
        }

        public static RectTransform Panel(string name, Transform parent, Color color, float weight = 0f,
                                          float radius = 0f)
        {
            var rect = NewRect(name, parent);
            var image = rect.gameObject.AddComponent<Image>();
            image.color = color;
            image.raycastTarget = false;
            Round(image, radius);
            if (weight > 0f) Weight(rect, weight);
            return rect;
        }

        // ── 文字：一個地方決定用哪個後端 ───────────────────────────────────────────
        //
        // 為什麼要有後端選擇：TMP 的畫質好，但它需要**字型資產**；而無頭環境裡
        // `TMP_FontAsset.CreateFontAsset(OS 字型)` 有可能回 null（實測就回了一次），
        // 那時畫面會**完全空白而沒有任何錯誤訊息**。所以這裡做一條回退：
        // 建不出 TMP 資產 ⇒ 整屏走 legacy `UnityEngine.UI.Text` ＋動態系統字型（CJK 一定有字）。
        // dump 會把用到的字型名寫出來 ⇒ 走哪一條一眼看得見。

        /// <summary>這一屏的文字後端（建立畫布時決定一次，整屏一致）。</summary>
        public enum TextBackend { Tmp, Legacy }

        private static TextBackend _backend = TextBackend.Tmp;
        private static TMP_FontAsset _tmpFont;
        private static Font _legacyFont;

        /// <summary>決定這一屏的文字後端（在建立任何文字之前呼叫一次）。</summary>
        public static void SelectTextBackend(Camera unused = null)
        {
            if (_tmpFont == null && _legacyFont == null) ResolveCjkFont();
            _backend = _tmpFont != null ? TextBackend.Tmp : TextBackend.Legacy;
            Debug.Log($"[screen] 文字後端＝{_backend}（TMP 字型＝{(_tmpFont != null ? _tmpFont.name : "<null>")}）");
        }

        /// <summary>
        /// 換掉之後新建文字用的 TMP 字型（Next 按界面語言換 Noto Sans JP／SC／TC，見 `NextFonts`）。
        /// 只影響之後建的字；`null` 不動（字型建不出來時照舊用 `ResolveCjkFont` 那一支）。
        /// </summary>
        public static void UseTmpFont(TMP_FontAsset font)
        {
            if (font == null || _backend != TextBackend.Tmp) return;
            _tmpFont = font;
        }

        /// <summary>目前用的是哪一個字型名（dump 用）。</summary>
        public static string FontName =>
            _backend == TextBackend.Tmp ? (_tmpFont != null ? _tmpFont.name : "<null>")
                                        : (_legacyFont != null ? _legacyFont.name : "<null>");

        /// <summary>
        /// 找字型：**拉丁 ＝ `DejaVu Sans`、中日韓回退 ＝ `Droid Sans Fallback`**——與真相那一側
        /// **逐字**一致（見下面）。
        ///
        /// 為什麼一定要這一步：TMP／UI 的預設字型沒有 CJK ⇒ 畫面上全是豆腐字，而**不會有任何錯誤訊息**
        /// （log 乾淨、dump 的文字也對——dump 讀的是欄位不是像素）。
        ///
        /// ⚠️ **兩個角色要分開**（owner 2026-10-12 拍板「2.換字體」）：真相那一側（無頭 chromium）
        /// 的 `font-family: 'Segoe UI', 'Noto Sans TC', 'Yu Gothic UI', system-ui, sans-serif`
        /// 在這台上一支都沒有 ⇒ 走到 fontconfig，而 fontconfig 是**逐字**選字：
        /// * 拉丁 ⇒ `DejaVu Sans`（`fc-match sans-serif`）
        /// * 中日韓 ⇒ `Droid Sans Fallback`（`fc-match sans-serif:lang=zh-tw`）
        /// ⇒ 這裡同一套：**`DejaVuSans.ttf` 當主字型，`DroidSansFallback.ttf` 掛在它的回退表**。
        /// 先前只用 Droid ⇒ 拉丁字形是 Droid 自己的（與 DejaVu 不同一套，`LOCAL`／`SYSTEM`／
        /// `READY` 這種全大寫標籤的寬度全部對不上）。
        ///
        /// ⚠️ **打包的那一份要和真相同一套**（owner 2026-10-11 拍板「字型打包」）：先前用建置機的
        /// Hiragino Sans，字形寬度不同 ⇒ 長段落多折 1～2 行、整頁被推下去（實測：服務頁 page hint
        /// 9 行 vs 真相 7 行、該頁上半 +16～19px）。產品端（Android）本來就沒有 Hiragino
        /// ⇒ 打包也是**唯一**能上真機的走法。
        /// 檔案：`Assets/Resources/Fonts/{DejaVuSans,DroidSansFallback}.ttf`（後者 4.0 MB、Apache-2.0）。
        /// </summary>
        private static void ResolveCjkFont()
        {
            var cjk = Resources.Load<Font>("Fonts/DroidSansFallback");
            var latin = Resources.Load<Font>("Fonts/DejaVuSans");

            // ① 產品自帶的字型——優先，因為它才是**與真相一致**的那一份
            if (latin != null)
            {
                // legacy `Text` 沒有「回退表」這種東西 ⇒ 那一條只能挑一支，挑 CJK 那支（有字優先）；
                // TMP 那一條才是真正的「拉丁 ＋ 回退」。
                _legacyFont = cjk ?? latin;
                var latinAsset = TryCreateTmpFont(latin);
                var cjkAsset = cjk != null ? TryCreateTmpFont(cjk) : null;
                if (latinAsset != null && cjkAsset != null && latinAsset != cjkAsset)
                {
                    if (latinAsset.fallbackFontAssetTable == null)
                        latinAsset.fallbackFontAssetTable = new System.Collections.Generic.List<TMP_FontAsset>();
                    if (!latinAsset.fallbackFontAssetTable.Contains(cjkAsset))
                        latinAsset.fallbackFontAssetTable.Add(cjkAsset);
                }
                _tmpFont = latinAsset ?? cjkAsset;
                // ⚠️ `TMP_FontAsset.name` 對執行期建出來的資產**是空的** ⇒ 要報「到底用哪一支」
                // 只能讀 `faceInfo.familyName`（否則 log 看起來像沒換到字型，只能靠像素猜）。
                Debug.Log($"[screen] 字型＝拉丁 {latin.name}{(latinAsset == null ? "（TMP 建不出來！）" : "")}" +
                          $" ＋ 回退 {(cjk != null ? cjk.name : "<無>")}{(cjk != null && cjkAsset == null ? "（TMP 建不出來！）" : "")}" +
                          $"；實際主字型＝{(_tmpFont != null ? _tmpFont.faceInfo.familyName : "<null>")}" +
                          $"／回退 {(_tmpFont != null && _tmpFont.fallbackFontAssetTable != null ? _tmpFont.fallbackFontAssetTable.Count : 0)} 支");
                return;
            }

            if (cjk != null)
            {
                Debug.Log($"[screen] 字型＝內建 {cjk.name}（Resources/Fonts；沒有 DejaVuSans ⇒ 拉丁也走它）");
                _legacyFont = cjk;
                _tmpFont = TryCreateTmpFont(cjk);
                return;
            }

            // ② 退回建置機／真機的系統字型（沒打包到字型時至少還有字可看）
            var preferred = new[]
            {
                "Hiragino Sans", "Hiragino Kaku Gothic ProN", "ヒラギノ角ゴシック",
                "Noto Sans CJK JP", "Noto Sans JP", "Source Han Sans JP", "YuGothic",
                "PingFang SC", "Heiti SC", "MS Gothic", "Meiryo",
            };
            var installed = new HashSet<string>(Font.GetOSInstalledFontNames());
            string chosen = null;
            foreach (var name in preferred)
            {
                if (installed.Contains(name)) { chosen = name; break; }
            }
            if (chosen == null)
            {
                var all = Font.GetOSInstalledFontNames();
                if (all != null && all.Length > 0) chosen = all[0];
                Debug.LogWarning($"[screen] 預期清單裡找不到 CJK 字型，改用 {chosen}");
            }
            if (chosen == null)
            {
                Debug.LogError("[screen] 這台機器一個系統字型都拿不到 ⇒ 畫面會空白");
                return;
            }

            _legacyFont = Font.CreateDynamicFontFromOSFont(chosen, 48);
            Debug.Log($"[screen] 系統字型＝{chosen}（legacy Font＝{(_legacyFont != null ? "ok" : "null")}）");
            _tmpFont = TryCreateTmpFont(_legacyFont);
        }

        /// <summary>
        /// 試著把一個 `Font` 建成 TMP 字型資產；**失敗一律吞掉回 `null`**（走 legacy `Text`）。
        ///
        /// ⚠️ 為什麼一定要 try/catch（2026-10-11 實測）：這個專案**沒有匯入 TMP Essential Resources**
        /// （`Assets/TextMesh Pro/Resources/TMP Settings.asset` 不在）⇒ `TMP_FontAsset.CreateFontAsset`
        /// 會從 `TMP_Settings.get_clearDynamicDataOnBuild` 丟 `NullReferenceException`，**整張屏建不起來**
        /// （不是回 null 那種安靜失敗）。要在這一條上只退後端、不要把整屏弄死。
        /// （要真的用 TMP：先匯入 TMP Essential Resources，再重驗行高／折行——見 `ResolveCjkFont` 的說明。）
        /// </summary>
        private static TMP_FontAsset TryCreateTmpFont(Font font)
        {
            try
            {
                var asset = TMP_FontAsset.CreateFontAsset(font);
                if (asset == null) Debug.LogWarning("[screen] TMP 資產回 null ⇒ 這一屏用 legacy Text");
                return asset;
            }
            catch (System.Exception e)
            {
                Debug.LogWarning($"[screen] TMP 資產建不出來（{e.GetType().Name}: {e.Message}）⇒ 這一屏用 legacy Text");
                return null;
            }
        }

        /// <summary>
        /// 一段文字。
        ///
        /// `noWrap`：**不要在自己這一格裡折行**（＝ CSS 的 `white-space: nowrap`／`flex-wrap` 容器的
        /// 子項行為）。什麼時候要它：斷行該發生在**項目之間**、不是字串自己折——例如 `.trpg-card`
        /// 那三格（真相是 flex 容器換行，每一格自己是一行）。UGUI 沒有 flex-wrap，若讓它自己折，
        /// 每一格都會被壓成兩行（實測：三格各兩行 vs 真相兩列）。
        /// </summary>
        public static Component Text(string name, Transform parent, string content, float size,
                                     Color? color = null, TextAlignmentOptions align = TextAlignmentOptions.TopLeft,
                                     float weight = 0f, bool noWrap = false, float lineRatio = 0f,
                                     float letterSpacing = 0f, bool bold = false)
        {
            var rect = NewRect(name, parent);
            var textColor = color ?? Color.white;
            var content2 = content ?? string.Empty;
            var ratio = lineRatio > 0f ? lineRatio : DesignTokens.TextLineRatio;
            // 行盒規格（`Settle`→`PinTextHeights` 讀；見 `LineBox`）：節點的高度＝行數 × 這一行的高。
            var box = rect.gameObject.AddComponent<LineBox>();
            box.Size = size;
            box.Ratio = ratio;
            // CSS 的每一行都把字**垂直居中**在自己的行盒裡（半行距）；legacy `Text` 的 `UpperLeft`
            // 是從頂端畫 ⇒ 要用 Middle*（否則墨跡會貼在行盒頂端、比真相高半行距）。
            align = CenterVertically(align);

            if (_backend == TextBackend.Tmp)
            {
                var tmp = rect.gameObject.AddComponent<TextMeshProUGUI>();
                tmp.font = _tmpFont;
                // `letter-spacing: .05em` ⇒ TMP 的 `characterSpacing`（單位＝**1/100 em**，逐字元、
                // 也計入 preferred 寬）。真相那一側**每個標籤幾乎都吃這個**（`.status-prefix`／
                // `.stage-index`／`.meter-cap`／`.chat-jump`…）⇒ 少了它全大寫標籤一律偏窄
                // （實測 `SYSTEM` 37 vs 57、`MOMO` 39 vs 47）。
                // ⚠️ 2026-10-12：**負的也要套**（原本寫 `> 0f` ⇒ 負值整條被丟掉）。
                // 真相有負值的地方：`.momotalk-brand strong { letter-spacing: -.05em }`（25px ⇒ 每個字
                // −1.25px，八個字＝**−10**）——不套的話 Unity 那邊的「MomoTalk」是 **137**（真相 131，
                // 字距還原後 ≈ 127），也就是**唯一**一條字距被吃掉的地方。
                if (Mathf.Abs(letterSpacing) > 0.0001f) tmp.characterSpacing = letterSpacing * 100f;
                // `font-weight: 800／700` ⇒ TMP 的**合成粗體**：字型資產只有 Regular 那一支，
                // 而真相那一側（無頭瀏覽器 ＋ Droid Sans Fallback）也是**瀏覽器自己合成的粗體**
                // ⇒ 兩邊是同一件事（都靠假粗體加寬一點）。
                if (bold) tmp.fontStyle = FontStyles.Bold;
                // 內嵌貼圖（`<sprite …>`）是舊殼的東西，素材已移除（2026-10-31）⇒ 一律退成純文字
                //（TMP 拿到 `<sprite>` 而沒有 sprite asset 會畫成字或 NRE）
                if (content2.IndexOf("<sprite", StringComparison.Ordinal) >= 0) content2 = StripSpriteTags(content2);
                tmp.text = content2;
                tmp.fontSize = size;
                tmp.color = textColor;
                tmp.alignment = align;
                tmp.textWrappingMode = noWrap ? TextWrappingModes.NoWrap : TextWrappingModes.Normal;
                tmp.overflowMode = TextOverflowModes.Overflow;
                tmp.raycastTarget = false;
                ApplyLineHeight(tmp, size, ratio);
                if (weight > 0f) Weight(rect, weight);
                return tmp;
            }

            var legacy = rect.gameObject.AddComponent<Text>();
            legacy.font = _legacyFont;
            legacy.text = content2;
            legacy.fontSize = Mathf.RoundToInt(size);
            legacy.color = textColor;
            legacy.alignment = LegacyAlign(align);
            legacy.horizontalOverflow = noWrap ? HorizontalWrapMode.Overflow : HorizontalWrapMode.Wrap;
            legacy.verticalOverflow = VerticalWrapMode.Overflow;
            legacy.raycastTarget = false;
            legacy.supportRichText = false;
            ApplyLineHeight(legacy, size, ratio);
            if (weight > 0f) Weight(rect, weight);
            return legacy;
        }

        /// <summary>
        /// **把行高調到 CSS 的目標比例**（owner 2026-10-11 拍板「A 動」）。
        ///
        /// 為什麼要：Unity 這一屏走 legacy `Text` ＋ Hiragino Sans（TMP 資產建不出來的回退），
        /// 它的自然行高比例 ≈ 0.92，而真相那一側（瀏覽器）≈ 1.33（12px ⇒ 16、10px ⇒ 13）
        /// ⇒ 每個文字塊都矮幾 px、**逐格累積**（實測：一行提示 8 vs 真相 14；背景頁整片 −6、角色頁 −12）。
        ///
        /// 兩條後端都吃同一個 `ratio`，但換算不同：
        /// * legacy：`lineSpacing` 是**倍率的增量**（0.1＝每行多 10%）⇒ `ratio / 自然比例 − 1`。
        /// * TMP：行距 = `lineHeight + lineSpacing × currentEmScale`，而 `currentEmScale = fontSize × 0.01`
        ///   （`TMP_Text.cs:4183`）⇒ `lineSpacing = (目標 − 自然) / (fontSize × 0.01)`。
        ///   （自然＝`faceInfo.lineHeight × fontSize / pointSize × faceInfo.scale`，`TMP_Text.cs:4181`。）
        /// </summary>
        private static void ApplyLineHeight(Component text, float size, float ratio)
        {
            if (text is Text legacy)
            {
                var font = legacy.font;
                if (font == null || font.fontSize <= 0) return;
                // ⚠️ `font.lineHeight / font.fontSize` 這個「自然行高比例」**只對 OS 動態字型有意義**
                //    （實測 Hiragino Sans：48/72 ⇒ 1.5）。換成**匯入的字型資產**（`Resources/Fonts/…`）
                //    時這兩個欄位的語意不一樣（實測會算出很大的比例）⇒ `lineSpacing` 變成大負值、
                //    **整段字行距被壓塌**（服務頁那條 page hint 變成一團 32px 高的糊字）。
                //    ⇒ 夾在真實字型的合理區間；比例異常就當 1.33（＝CSS normal）用，並留 log 供下一輪校準。
                var natural = font.lineHeight / (float)font.fontSize;
                if (natural < 1.0f || natural > 1.8f)
                {
                    Debug.Log($"[screen] 行高比例異常（{font.name}: lineHeight={font.lineHeight} " +
                              $"fontSize={font.fontSize} ⇒ {natural:F3}）⇒ 當 {DesignTokens.TextLineRatio:F2} 用");
                    natural = DesignTokens.TextLineRatio;
                }
                // 可為負：CSS 的行高比字型的自然行高**小**時要往下壓（例：卡標題 normal ⇒ 1.33）
                legacy.lineSpacing = ratio / natural - 1f;
                return;
            }
            if (text is TMPro.TMP_Text tmp)
            {
                var font = tmp.font;
                if (font == null) return;
                var info = font.faceInfo;
                if (info.pointSize <= 0f) return;
                var em = size * 0.01f;                       // ＝ TMP 的 currentEmScale
                var natural = info.lineHeight * size / info.pointSize * info.scale;
                tmp.lineSpacing = (size * ratio - natural) / em;
            }
        }

        private static TextAnchor LegacyAlign(TextAlignmentOptions align)
        {
            switch (align)
            {
                case TextAlignmentOptions.Center: return TextAnchor.MiddleCenter;
                case TextAlignmentOptions.Right: return TextAnchor.MiddleRight;
                case TextAlignmentOptions.MidlineRight: return TextAnchor.MiddleRight;
                case TextAlignmentOptions.BottomLeft: return TextAnchor.LowerLeft;
                case TextAlignmentOptions.TopLeft: return TextAnchor.UpperLeft;
                default: return TextAnchor.MiddleLeft;
            }
        }

        /// <summary>
        /// **把垂直靠上換成垂直居中**（`TopLeft`→`Left`、`Top`→`Center`、`TopRight`→`Right`）。
        ///
        /// 為什麼：CSS 的每一行都把字居中在自己的行盒裡（上下各半個行距），而 UGUI 的 `UpperLeft`
        /// 是**從頂端畫**——現在節點高度＝行盒高度（`LineBox`）⇒ 不換就會讓墨跡貼在行盒頂端、
        /// 比真相高半個行距（9px 提示＝3px、12px 卡標題＝2.5px）。
        /// </summary>
        private static TextAlignmentOptions CenterVertically(TextAlignmentOptions align)
        {
            switch (align)
            {
                case TextAlignmentOptions.TopLeft: return TextAlignmentOptions.Left;
                case TextAlignmentOptions.Top: return TextAlignmentOptions.Center;
                case TextAlignmentOptions.TopRight: return TextAlignmentOptions.Right;
                default: return align;
            }
        }

        /// <summary>
        /// `BA_DUMP_ALL=1` ⇒ dump **連沒有字的容器**也一起列（`#node:` 開頭；見 `Collect`）。
        /// 預設關（判準檔＝文字那一桶，加容器會讓 `ab_compare.py` 的「條數」對不上）。
        /// </summary>
        private static readonly bool AllNodes =
            System.Environment.GetEnvironmentVariable("BA_DUMP_ALL") == "1";

        /// <summary>
        /// **文字 dump**——把畫面上的文字照階層順序收成一份清單（M3 對拍用的機械判據）。
        ///
        /// 為什麼要機械收而不是人手寫清單：手寫的清單會漏掉「程式碼有寫但沒顯示」的東西，
        /// 而那種東西在並排截圖上往往看不出來。dump 是**從畫出來的物件**倒推的。
        /// 格式：`路徑|<文字>|<寬x高>`（路徑＝階層名，`/` 分隔）——漂動時一眼看得出是哪一格。
        /// </summary>
        public static string Dump(Transform root)
        {
            var lines = new List<string>();
            Collect(root, string.Empty, lines, root as RectTransform);
            return string.Join("\n", lines) + (lines.Count > 0 ? "\n" : string.Empty);
        }

        private static readonly Dictionary<string, Texture2D> _iconTextures = new Dictionary<string, Texture2D>();

        /// <summary>
        /// 一個 **SVG 圖示**（真相是 `<svg class="ico"><use href="#i-…">`）——Unity 這一側是預烤好的
        /// PNG（`Assets/Resources/icons/i-*.png`，由 `tools/bake_icons.py` 從真相同一套 SVG／CSS 烤的）。
        ///
        /// ⚠️ 顏色**不在 PNG 裡**（烤的是白色）⇒ 由 `tint` 給：真相的圖示一律 `stroke: currentColor`
        /// ⇒ 顏色跟著它所在那一格的 CSS（`.tile-icon` #198ac4／`.menu-close` #173558／
        /// `.tile-more` #8ba7bd／`.menu-back` #2c4a70…）。
        /// ⚠️ 用 `RawImage` 不用 `Image`：本專案不進版控 `.meta` ⇒ PNG 的匯入型別不會是 Sprite
        /// （`Resources.Load<Sprite>` 會拿到 null）。圖示是正方形 ⇒ 不需要 preserveAspect。
        /// 位置：預設**置中**（`place-items: center` 那一族的做法）；掛在有 LayoutGroup 的父層時，
        /// 位置由排版決定、這裡的 anchor 不影響。
        /// </summary>
        public static Component Icon(string node, string icon, Transform parent, float size, Color tint,
                                     float weight = 0f)
        {
            var go = new GameObject(node, typeof(RectTransform), typeof(RawImage));
            var rect = (RectTransform)go.transform;
            rect.SetParent(parent, false);
            var image = go.GetComponent<RawImage>();
            image.texture = IconTexture(icon);
            image.color = tint;
            image.raycastTarget = false;
            rect.anchorMin = rect.anchorMax = new Vector2(0.5f, 0.5f);
            rect.pivot = new Vector2(0.5f, 0.5f);
            rect.anchoredPosition = Vector2.zero;
            if (weight > 0f) Weight(rect, weight);
            else
            {
                FixedWidth(rect, size);
                FixedHeight(rect, size);
                // ⚠️ `FixedWidth/FixedHeight` **只宣告 `LayoutElement`**（給有 LayoutGroup 的父層用）
                //    ⇒ 父層不是 LayoutGroup 時（圖示框、右上角那顆 ✕…）尺寸要看 `sizeDelta`
                //    （第一次漏了這一行：RawImage 的預設 sizeDelta 是 100×100 ⇒ 圖示爆框、還偏位）。
                rect.sizeDelta = new Vector2(size, size);
            }
            return image;      // Component（呼叫端用 `.transform` 拿 RectTransform）
        }

        private static Texture2D IconTexture(string name)
        {
            if (_iconTextures.TryGetValue(name, out var hit)) return hit;
            var texture = Resources.Load<Texture2D>("icons/i-" + name);
            if (texture == null)
                Debug.LogWarning($"[screen] 圖示烤檔載不到：icons/i-{name}.png ⇒ 這一格不畫圖" +
                                 "（要重烤：`.venv-cdp/bin/python tools/bake_icons.py`）");
            _iconTextures[name] = texture;
            return texture;
        }

        /// <summary>
        /// 把 `<sprite name="…">` 拿掉（留一個空格，免得前後的字黏在一起）。
        ///
        /// 什麼時候用：`Text()` 看到 `<sprite` 就退成純文字——內嵌貼圖是舊殼的東西，素材已移除（2026-10-31）；
        /// TMP 拿到 `<sprite>` 而沒有可用的 sprite asset 會畫成字或直接 NRE。
        /// </summary>
        private static string StripSpriteTags(string text) =>
            System.Text.RegularExpressions.Regex.Replace(text ?? "", "<sprite[^>]*>", " ");

        /// <summary>
        /// **把一段外部來的字變成「純文字」**（TMP 的 rich text 不會把裡面的 `&lt;b&gt;`／`&lt;color&gt;` 當標籤）。
        /// 真相那一側是 `textContent`（一律純文字）——她的回覆／他打的字裡出現 `&lt;` 時，TMP 不跳脫就會
        /// 把它吃成標籤（字消失或變色）。做法：在 `&lt;` 後面塞一個零寬空白（看不見、標籤也成不了）。
        /// </summary>
        public static string Plain(string text) =>
            string.IsNullOrEmpty(text) ? (text ?? string.Empty) : text.Replace("<", "<​");

        /// <summary>
        /// **dump 用的「看得到的字」**：把 TMP 的 `<sprite …>` 標記拿掉。
        ///
        /// 為什麼：真相那一側的內嵌貼圖是 `<img>`，`innerText` **不計**它 ⇒ 只留前後文字
        /// （`早安 <sprite name="001_yes"> 走吧` ⇒ `早安  走吧`，與真相逐字相同）。
        /// 少了這一條，`sticker_mixed` 那一格會被判成「多一條／少一條」。
        ///
        /// ⚠️ 2026-10-12：改成**通吃所有標籤**（原本只吃 `<sprite …>`）。因為同一行裡要做
        /// **行內字級差**（真相的 `.menu-heading` 是 `<span>選單 <small>MENU</small></span>`：
        /// 標題 20px、小字 9px／`#7196ae`，但 `innerText` 是**同一行**）——TMP 那邊只能用
        /// `<size=9><color=#7196ae>` 做，而 dump／A/B 比的必須是不帶標籤的純文字。
        /// 等價於 TMP 的 `GetParsedText()`，但不依賴它有沒有解析過（batchmode 時序難保證）。
        /// </summary>
        private static readonly Regex SpriteTag = new Regex("<[^>]+>", RegexOptions.Compiled);

        private static string VisibleText(string raw) =>
            string.IsNullOrEmpty(raw) ? raw : SpriteTag.Replace(raw, string.Empty);

        /// <summary>
        /// 這一格在**畫布設計座標**裡的位置（`x,y` ＝ **左上角**、**y 往下**——與真相的
        /// `getBoundingClientRect()` 同一套座標，可以直接跟 `webui_dump.sh --rects` 對照）。
        ///
        /// 為什麼要它：原本只 dump「多大」，說不出「在哪」——而**位置全錯、文字 dump 照樣全綠**
        /// （dump 走 transform 樹，畫到畫布外的字也照樣列出來）。2026-10-11 就是這樣吃了一次：
        /// 設定面板整片被畫到畫布上緣之外（偏上 152）、A/B 37/37 綠，只有像素量測看得出來。
        /// 有了這一欄，「量框」這件事才算對稱（真相側本來就有 `--rects`）。
        /// </summary>
        private static Vector4 DesignRect(RectTransform canvas, RectTransform rect)
        {
            if (canvas == null) return Vector4.zero;
            var width = canvas.rect.width;
            var height = canvas.rect.height;
            // 畫布 pivot 是 0.5 ⇒ 畫布左緣＝ `-width/2`、上緣＝ `+height/2`（local 座標）。
            var topLeft = rect.TransformPoint(new Vector3(rect.rect.xMin, rect.rect.yMax, 0f));
            var local = canvas.InverseTransformPoint(topLeft);
            return new Vector4(local.x + width * 0.5f, height * 0.5f - local.y,
                               rect.rect.width, rect.rect.height);
        }

        private static void Collect(Transform node, string path, List<string> lines, RectTransform canvas)
        {
            var here = path.Length == 0 ? node.name : path + "/" + node.name;
            foreach (Transform child in node)
            {
                var content = (string)null;
                var tmp = child.GetComponent<TextMeshProUGUI>();
                if (tmp != null) content = VisibleText(tmp.text);
                var legacy = child.GetComponent<Text>();
                if (legacy != null) content = legacy.text;

                if (child.name.StartsWith("#control:"))
                {
                    // 控制項的值單獨一行（`#control:<key>|<值>`）：**不進文字那一桶**，
                    // 由 `ab_compare.py` 拿真相側的 `.values.txt` 跟它對（見 `ControlValue`）。
                    lines.Add($"{child.name}|{content ?? string.Empty}");
                }
                else if (!string.IsNullOrEmpty(content))
                {
                    var rect = (RectTransform)child;
                    var size = rect.rect.size;
                    // 量測值（`pref`）一起 dump：**分得出「葉子量到 0」與「容器沒分配」**——
                    // 這次設定頁行高全 0 就是靠這欄才定位（見 §5.3 的檢修筆記）。
                    var prefW = LayoutUtility.GetPreferredWidth(rect);
                    var prefH = LayoutUtility.GetPreferredHeight(rect);
                    var design = DesignRect(canvas, rect);
                    lines.Add($"{here}/{child.name}|{content.Replace("\n", "\\n")}" +
                              $"|{size.x:0}x{size.y:0}|pref {prefW:0}x{prefH:0}" +
                              $"|rect {design.x:0},{design.y:0} {design.z:0}x{design.w:0}");
                }
                else if (AllNodes)
                {
                    // `BA_DUMP_ALL=1`：**連沒有字的容器也列**（`#node:<路徑>|rect x,y WxH`）。
                    // 為什麼要它：文字那一桶只有「有字的葉子」，「哪個容器落在 y 多少」問不出來
                    // ⇒ 追設定面板那 1～3px 的偏移時只能從像素反推（2026-10-11）。這一行補上那一格。
                    // 預設關閉 ⇒ 既有工具（`ab_compare.py`／`rect_audit.py`）看到的檔案完全一樣。
                    var box = (RectTransform)child;
                    var where = DesignRect(canvas, box);
                    lines.Add($"#node:{here}/{child.name}|rect {where.x:0},{where.y:0} " +
                              $"{where.z:0}x{where.w:0}");
                }
                Collect(child, here, lines, canvas);
            }
        }

    /// <summary>
    /// **純裝飾標記**（邊線／外框環／陰影條這一類，畫面要看得到、但**不該接點擊**）。
    ///
    /// 為什麼要有（2026-10-20）：觸控路由的命中判定看的是**矩形**、不是貼圖像素——
    /// `Border(..., radius)` 那一圈是「整個面板那麼大、中間透明」的九宮格環，
    /// 而 `EdgeLine` 會 `SetAsLastSibling()`（畫在最上面）⇒ 兩者都會把面板裡的每一擊吃掉。
    /// 實測症狀：MomoTalk 檢索欄怎麼點都只命中 `Border ← TalkPanel`（＝打不進字）。
    /// ⇒ 這類節點掛上本標記，`ShellRouter.HitTest` 一律跳過它、繼續往下面那一層找。
    /// </summary>
    public sealed class Decor : MonoBehaviour
    {
    }
    }
}
