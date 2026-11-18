using BlueAgent.Logic;
using BlueAgent.Screens;
using UnityEngine;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>
    /// 聊天頁上方的**立繪舞台**（B2）＋**場景背景**（P3）。
    ///
    /// 背景那一層照舊壳的規則（`HomeState` ＋ `BackgroundLibrary`）：
    ///   ① 場景包 = `Resources/backgrounds/&lt;id&gt;/background.json`（`image`／`dim`／`variants`）；
    ///   ② 挑圖順序＝**按時段挑 variant**（dawn/morning/noon/dusk/night，05-08／08-11／11-16／16-19／19-05），
    ///      挑不到才退回 `image`；
    ///   ③ `dim` 是壓暗百分比（0-60），畫成一層黑。
    ///
    /// 立繪邏輯不重寫：`StageAvatar`（角色卡 images/animations、眨眼、嘴型時間軸）原樣用。
    /// </summary>
    public static class NextStageView
    {
        /// <summary>舞台在設計格（460×818）裡的高度；聊天區優先，所以給 ~30%。</summary>
        public const float StageHeight = 320f;

        /// <summary>
        /// 1:1 聊天頁的舞台高（owner 2026-11-07「调整舞台高度」；PLAN-chat-list §4.2 定的 Entity ⇒ 224）。
        /// 320 時舞台約佔半屏、只看得到兩三則訊息；通話頁仍用 <see cref="StageHeight"/>。
        /// </summary>
        public const float ChatStageHeight = 224f;

        /// <param name="fill">
        /// 舞台吃掉父欄剩下的高度（大螢幕聊天頁的左欄，owner 2026-10-30）；否則固定 <see cref="StageHeight"/>。
        /// 立繪的大小由 `StageAvatar.Fit()` 照框高每幀算，框多高都不會拉扁。
        /// </param>
        /// <summary>
        /// 把舞台框成一張卡（設計稿 v5 ②）：半透明圓角底＋細邊。不框的話舞台與下面的訊息是同一片底，
        /// 往上捲時氣泡像是被舞台直接切掉（owner 2026-11-07）。底板放最底層，立繪／3D／按鈕都在它上面。
        /// </summary>
        public static void Frame(RectTransform holder)
        {
            if (holder == null || holder.Find("StageFrameBg") != null) return;
            var plate = UiBuilder.Backdrop("StageFrameBg", holder, new Color(1f, 1f, 1f, 0.5f),
                                           radius: NextTheme.Radius);
            plate.SetAsFirstSibling();
            var edge = UiBuilder.Overlay("StageFrameEdge", holder);
            UiBuilder.Border(edge, NextTheme.PanelLine, 1f, NextTheme.Radius);
        }

        public static StageAvatar Build(RectTransform parent, string character, string state, string backgroundId,
                                        bool fill = false, NextShell moodOf = null, string entity = "",
                                        float height = StageHeight)
        {
            var holder = UiBuilder.Row("Stage", parent, 0f, null, TextAnchor.MiddleCenter);
            if (fill)
            {
                UiBuilder.Weight(holder, 1f, min: 200f);
            }
            else
            {
                UiBuilder.FixedHeight(holder, height > 0f ? height : StageHeight);
                UiBuilder.Weight(holder, 0f);
            }

            // ① 背景（不參與排版、鋪滿舞台；raycast 全關，不然會擋到上面的按鈕）
            var bgGo = new GameObject("StageBg", typeof(RectTransform), typeof(Image));
            var bgRect = (RectTransform)bgGo.transform;
            bgRect.SetParent(holder.transform, false);
            bgGo.AddComponent<LayoutElement>().ignoreLayout = true;
            Stretch(bgRect);
            var bgImage = bgGo.GetComponent<Image>();
            bgImage.raycastTarget = false;
            bgImage.preserveAspect = false;         // 場景圖是「鋪滿裁切」，不是 contain
            bgImage.enabled = false;

            // ② 壓暗層（dim）
            var dimGo = new GameObject("StageDim", typeof(RectTransform), typeof(Image));
            var dimRect = (RectTransform)dimGo.transform;
            dimRect.SetParent(holder.transform, false);
            dimGo.AddComponent<LayoutElement>().ignoreLayout = true;
            Stretch(dimRect);
            var dimImage = dimGo.GetComponent<Image>();
            dimImage.raycastTarget = false;
            dimImage.color = new Color(0f, 0f, 0f, 0f);
            dimImage.enabled = false;

            ApplyBackground(bgImage, dimImage, backgroundId);

            // ③ 立繪（最上層）
            var go = new GameObject("StageAvatar", typeof(RectTransform), typeof(Image));
            var image = go.GetComponent<Image>();
            image.preserveAspect = true;
            image.raycastTarget = false;
            image.color = new Color(1f, 1f, 1f, 0f);

            var rect = (RectTransform)go.transform;
            rect.SetParent(holder.transform, false);
            rect.anchorMin = Vector2.zero;
            rect.anchorMax = Vector2.one;
            rect.offsetMin = Vector2.zero;
            rect.offsetMax = Vector2.zero;

            var avatar = go.AddComponent<StageAvatar>();
            // ⚠️ 這一顆的尺寸只由 `StageAvatar.Fit()`（依貼圖長寬比 contain 進框）決定。
            // 父層是 `Row`（帶 LayoutGroup），若讓它參與排版，每一輪 layout 都會把高度改回框高，
            // `Fit()` 下一幀又改回來 ⇒ 立繪每幀 236↔248 抖動（實測 watchdog 連印 5 行「立繪が跳んだ」）。
            var layout = go.AddComponent<LayoutElement>();
            layout.ignoreLayout = true;
            avatar.Bind(image, View(character, state));

            // ④ 3D 模式（有模型的角色才有切換鈕；owner 2026-10-30）
            if (NextStage3D.Has(character)) AddModeSwitch(holder, go, character, state);
            // ⑤ 平台公開心情徽章：主人與客人都看自己的可見域。
            if (moodOf != null) NextMoodBadge.Attach(holder, moodOf, entity);
            return avatar;
        }

        /// <summary>
        /// 右上角的 2D／3D 切換鈕。3D 那層蓋在立繪上面；切回 2D 就整層拆掉（模型、相機、RT 一起放掉），
        /// 立繪那顆照常活著（說話狀態、眨眼都不斷），只是用 `CanvasGroup` 藏起來。
        /// 選擇存在本機（`NextConfig.Stage3D`），換頁／換角色重建時沿用。
        /// </summary>
        private static void AddModeSwitch(RectTransform holder, GameObject flat, string character, string state)
        {
            var flatGroup = flat.AddComponent<CanvasGroup>();
            flatGroup.blocksRaycasts = false;
            NextStage3D live = null;

            var button = NextUi.Button("StageMode", holder, "3D", NextUi.Style.Secondary,
                                       height: 26f, width: 46f, skew: false, fontSize: NextTheme.NoteSize);
            var rect = (RectTransform)button.transform;
            var element = rect.GetComponent<LayoutElement>();
            if (element == null) element = rect.gameObject.AddComponent<LayoutElement>();
            element.ignoreLayout = true;
            rect.anchorMin = rect.anchorMax = rect.pivot = Vector2.one;
            rect.sizeDelta = new Vector2(46f, 26f);
            rect.anchoredPosition = new Vector2(-8f, -8f);
            var label = NextUi.LabelOf(button);

            void Show(bool use3d)
            {
                flatGroup.alpha = use3d ? 0f : 1f;
                if (use3d && live == null)
                {
                    live = NextStage3D.Create(holder, character, state);
                    live.Failed += () =>
                    {
                        NextConfig.Stage3D = false;
                        live = null;
                        Show(false);
                    };
                }
                else if (!use3d && live != null)
                {
                    Object.Destroy(live.gameObject);
                    live = null;
                }
                rect.SetAsLastSibling();                 // 鈕永遠在最上層
                if (label != null) label.text = use3d ? "2D" : "3D";   // 顯示「按了會切到哪一種」
            }

            button.onClick.AddListener(() =>
            {
                NextConfig.Stage3D = live == null;
                Show(NextConfig.Stage3D);
            });
            Show(NextConfig.Stage3D);
        }

        /// <summary>
        /// 把場景包畫到舞台：`empty` ⇒ 兩層都關（純底色）。
        /// ⚠️ 順序照舊壳踩過的坑：**先鋪 UI 再點亮背景圖**，避免第一幀閃黑。
        /// </summary>
        public static void ApplyBackground(Image bgImage, Image dimImage, string backgroundId)
        {
            if (bgImage == null) return;
            if (string.IsNullOrEmpty(backgroundId))
            {
                bgImage.enabled = false;
                if (dimImage != null) dimImage.enabled = false;
                return;
            }

            var card = Resources.Load<TextAsset>("backgrounds/" + backgroundId + "/background");
            if (card == null)
            {
                Debug.LogWarning("[next] 場景包讀不到：" + backgroundId);
                bgImage.enabled = false;
                if (dimImage != null) dimImage.enabled = false;
                return;
            }

            var json = new JObject(card.text);
            var slot = BackgroundSlot.ForHour(System.DateTime.Now.Hour);
            var file = json.OptString("image");
            var variants = json.OptJArray("variants");
            for (var i = 0; variants != null && i < variants.Length(); i++)
            {
                var row = variants.OptJObject(i);
                if (row != null && row.OptString("slot") == slot)
                {
                    var variantImage = row.OptString("image");
                    if (!string.IsNullOrEmpty(variantImage)) file = variantImage;
                    break;
                }
            }

            var texture = LoadTexture("backgrounds/" + backgroundId + "/" + StripExtension(file));
            if (texture == null)
            {
                Debug.LogWarning("[next] 場景圖讀不到：" + backgroundId + "/" + file);
                bgImage.enabled = false;
                if (dimImage != null) dimImage.enabled = false;
                return;
            }

            // 貼圖是 Resources 的共用資產（不歸這裡）；Sprite 是每次重建聊天頁新做的 ⇒ 歸 bgImage 所有
            NextUi.SetOwnedSprite(bgImage, texture, ownTexture: false);
            bgImage.color = Color.white;
            bgImage.enabled = true;

            if (dimImage != null)
            {
                var dim = Mathf.Clamp(json.OptInt("dim"), 0, 80) / 100f;
                dimImage.color = new Color(0f, 0f, 0f, dim);
                dimImage.enabled = dim > 0f;
            }
            Debug.Log($"[next] 背景 {backgroundId}／slot={slot}／{file}／dim={json.OptInt("dim")}");
        }

        private static Texture2D LoadTexture(string path)
        {
            var texture = Resources.Load<Texture2D>(path);
            if (texture != null) return texture;
            // 有些圖是 Sprite 匯入（Texture Type = Sprite）⇒ 從 Sprite 取
            var sprite = Resources.Load<Sprite>(path);
            return sprite != null ? sprite.texture : null;
        }

        private static string StripExtension(string file)
        {
            if (string.IsNullOrEmpty(file)) return file;
            var dot = file.LastIndexOf('.');
            return dot > 0 ? file.Substring(0, dot) : file;
        }

        private static void Stretch(RectTransform rect)
        {
            rect.anchorMin = Vector2.zero;
            rect.anchorMax = Vector2.one;
            rect.offsetMin = Vector2.zero;
            rect.offsetMax = Vector2.zero;
        }

        public static JObject View(string character, string state)
        {
            return new JObject()
                .Put("character", string.IsNullOrEmpty(character) ? "mira" : character)
                .Put("state", string.IsNullOrEmpty(state) ? AvatarTimeline.Normal : state);
        }
    }
}
