using BlueAgent.Logic;
using UnityEngine;
using UnityEngine.UI;

namespace BlueAgent.Screens
{
    /// <summary>
    /// **舞台那格的立繪層**（真相 `app/assets/webui/app.js` 的 `paintAvatar` ＋ `avatar.js`）。
    ///
    /// 切到誰就畫誰的動態立繪（與 webui 同一條邏輯）。Live2D 已移除（owner 2026-10-30：
    /// 「Live2D 路線直接放棄」）；3D 立繪是 Next 舞台上另一層（`NextStage3D`），不經過這裡。
    ///
    /// 真相那條（`paintAvatar`）逐字：
    /// <code>
    ///   const imageState = next.state === 'thinking' ? 'thinking'
    ///     : next.state === 'speaking' ? 'speaking' : 'normal';
    ///   if (avatarCharacter !== profile.id) { avatar.setCharacter(profile.id); }
    ///   avatar.setState(imageState);
    /// </code>
    /// 換角色只在**真的換了**才叫（`avatarCharacter !== profile.id`）——同一條在 `AvatarTimeline`
    /// 裡也有（memo by key），這裡照抄是為了不白白重建。
    /// </summary>
    public sealed class StageAvatar : MonoBehaviour
    {
        /// <summary>
        /// 上一次印過的立繪憑據（換角色／換圖／換框才會再印）。
        ///
        /// ⚠️ 為什麼要這個：這行 log 原本**每次重建都印**（實測 400ms 內印 4 次），
        /// logcat 的緩衝區會被灌滿 ⇒ 取證時**早期的點擊記錄被沖掉**（2026-10-17 整輪的
        /// 卡片點擊證據就是這樣消失的）。改成「值變了才印」，驗收照樣拿得到數字。
        /// </summary>
        private string _logged;

        Image _image;
        AvatarTimeline _timeline;
        Texture2D _texture;
        Sprite _sprite;
        string _character = "";
        string _state = "";
        double _nowMs;
        bool _fitted;

        /// <summary>真相 `max-width: min(100%, 285px)`。</summary>
        const float MaxPortraitWidth = 285f;

        /// <summary>真相 `max-height: calc(100% - 12px)`。</summary>
        const float PortraitMargin = 12f;

        /// <summary>
        /// **現在這一個**（事件流要餵嘴型時找它——`BridgeShell` 的 `lip` 幀）。
        /// 一個殼只有一格舞台，所以一個就夠；重畫時新的會把它蓋掉。
        /// </summary>
        public static StageAvatar Current;

        /// <summary>接線：立繪那顆 `Image`（`ShellView` 的舞台／`NextStageView`）。</summary>
        public void Bind(Image image, JObject view)
        {
            Current = this;
            _image = image;
            Apply(view);
        }

        /// <summary>投影下來了（每一輪 state 都會叫）——角色／狀態有變才換圖。</summary>
        public void Apply(JObject view)
        {
            if (view == null || _image == null) return;
            var id = view.OptString("character");

            if (_timeline == null || id != _character)
            {
                _character = id;
                _timeline = new AvatarTimeline(Card(id), id);
                _state = "";                       // 換角色 ⇒ 狀態一定要重設（回去 normal）
            }

            var state = ImageState(view);
            if (state != _state)
            {
                _state = state;
                _timeline.SetState(state);
            }
            Show(_timeline.Src);
        }

        /// <summary>
        /// `lip` 訊框（webui 的 `applyLip`）——**只在 `speaking` 時吃**，
        /// 而且**嘴型框瞬間換**（見 `AvatarTimeline` 的註解：交叉淡入會疊影）。
        /// 回 <c>true</c>＝這一格有圖可畫。
        ///
        /// </summary>
        public bool ApplyLip(string mouth)
        {
            if (_timeline == null || !_timeline.ApplyLip(mouth)) return false;
            Show(_timeline.Src);
            return true;
        }

        void OnDestroy()
        {
            if (Current == this) Current = null;
        }

        // ── 見張り：立繪が一フレームで跳んだら記錄する（owner 2026-10-19「立绘有时候会突发性的剧烈的上下抖动一下，
        //    没有找到规律」——これで原因を捕まえた。直った後も回歸の番として殘す；跳ばなければ何も出さない）。
        //    舞台の作り直しで別インスタンスになっても比べられるよう static。
        static Vector3 _seenPos = new Vector3(float.NaN, 0f, 0f);
        static Vector2 _seenSize;
        static int _seenOwner;
        static int _serials;
        readonly int _serial = ++_serials;
        static bool _seenShown;

        void LateUpdate()
        {
            if (_image == null) return;
            // ⚠️ 原因（2026-10-19 真機、この監視で捕まえた）：舞台は狀態が變わるたびに作り直され、新しい立繪は
            //    **最初のフレームに `Update` が呼ばれない**（そのフレームの途中で生まれた component）⇒ Fit されないまま
            //    舞台の框いっぱい（342×555、中央）で一フレーム描かれ、次のフレームで 342×341（下寄せ）に戻る＝「ガクッ」。
            //    LateUpdate は同じフレームの描畫前に來る ⇒ ここで合わせる。框の大きさが變わったときも合わせ直す。
            var parent = transform.parent as RectTransform;
            if (_image.gameObject.activeSelf && _texture != null
                && (!_fitted || (parent != null && parent.rect.size != _fitBox))) Fit();
            if (_image.gameObject.activeSelf) _image.enabled = _fitted;      // 合わせられないうちは描かない（ずれた一フレームより空白）
            var rect = (RectTransform)transform;
            var box = transform.parent as RectTransform;
            var shown = _image.isActiveAndEnabled && _image.sprite != null;
            var pos = rect.position;
            var size = rect.rect.size;
            var me = _serial;
            if (!float.IsNaN(_seenPos.x) && shown && _seenShown
                && (Mathf.Abs(pos.y - _seenPos.y) > 3f || Mathf.Abs(size.y - _seenSize.y) > 3f))
            {
                Debug.Log($"[stage] 立繪が跳んだ：y {_seenPos.y:F0}→{pos.y:F0} 高 {_seenSize.y:F0}→{size.y:F0}"
                          + $"｜box={(box != null ? box.rect.size.ToString("F0") : "?")}｜fitted={_fitted}"
                          + $"｜{(me != _seenOwner ? "別インスタンス（舞台を作り直した）" : "同じインスタンス")}"
                          + $"｜frame={Time.frameCount}");
            }
            _seenPos = pos;
            _seenSize = size;
            _seenOwner = me;
            _seenShown = shown;
        }

        void Update()
        {
            if (_timeline == null || !_image.gameObject.activeSelf) return;
            if (!_fitted) Fit();                                 // 版面結算完才量得到框，這裡每幀補
            _nowMs += Time.unscaledDeltaTime * 1000.0;          // 毫秒、不受 timeScale 影響
            var before = _timeline.Src;
            _timeline.Advance(_nowMs);
            if (_timeline.Src != before) Show(_timeline.Src);
        }

        /// <summary>
        /// 立繪該畫哪一種狀態——**逐字照 webui 的 `paintAvatar`**：
        /// <code>
        ///   next.state === 'thinking' ? 'thinking' : next.state === 'speaking' ? 'speaking' : 'normal'
        /// </code>
        /// （owner 2026-10-16 拍板「讓投影帶 state，和 webui 一致」⇒ 投影現在帶這個鍵，
        /// 不必再從別處推。）
        /// </summary>
        static string ImageState(JObject view)
        {
            var state = view.OptString("state");
            if (state == AvatarTimeline.Thinking) return AvatarTimeline.Thinking;
            if (state == AvatarTimeline.Speaking) return AvatarTimeline.Speaking;
            return AvatarTimeline.Normal;
        }

        /// <summary>角色卡（`Assets/Resources/characters/&lt;id&gt;/character.json`）——`images`／`animations` 的來源。</summary>
        static string Card(string id)
        {
            if (string.IsNullOrEmpty(id)) return "";
            var asset = Resources.Load<TextAsset>("characters/" + id + "/character");
            return asset != null ? asset.text : "";
        }

        void Show(string src)
        {
            var texture = Load(src);
            if (texture == null || texture == _texture) return;   // 缺圖 ⇒ 留著上一張（avatar.js 也是）
            _texture = texture;
            // 貼圖 → Sprite（`Image` 吃 Sprite；`preserveAspect` 才會生效）
            _sprite = Sprite.Create(texture,
                new Rect(0f, 0f, texture.width, texture.height), new Vector2(0.5f, 0.5f));
            _image.sprite = _sprite;
            _image.color = Color.white;
            _fitted = false;                        // 合わせるのは同じフレームの LateUpdate（ここでは框がまだ仮の大きさ）
        }

        /// <summary>
        /// **立繪的縮放**——真相 `styles.css:209`：
        /// <code>
        ///   .avatar-stack img, #portrait { position:absolute; inset:0; margin:auto;
        ///     width:auto; height:auto; max-width: min(100%, 285px);
        ///     max-height: calc(100% - 12px); object-fit: contain; }
        /// </code>
        /// 也就是 **整張按比例縮進框裡**（contain，不是 cover！），左右上限是框寬與 **285px 取小**、
        /// 上下限是 **框高 − 12px**（上下各留 6px），**居中**。
        /// ⚠️ 真相的 CSS 註解寫得很清楚：**不要寫死寬度**——框一矮（遊戲模式舞台只剩一半高）
        /// 圖就會上下被切掉（owner 2026-10-03 抓到的「人臉被截斷」）。
        /// </summary>
        /// <summary>設計格（460×818）下立繪框的高：舞台 384 − slot 上下內縮 2＋23。</summary>
        const float DesignBoxHeight = 359f;

        void Fit()
        {
            if (_texture == null) return;
            var box = transform.parent as RectTransform;
            if (box == null) return;
            var boxWidth = box.rect.width;
            var boxHeight = box.rect.height;
            if (boxWidth <= 1f || boxHeight <= 1f) return;      // 版面還沒結算，下一幀再來

            // 尺寸**只由貼圖自己決定**（上限：寬 min(框寬, 285)、高 框高 − 12）——
            // 與父框的形狀無關，所以父框怎麼變都不會把圖拉扁。
            // 長螢幕（舞台比設計格高，見 `UiBuilder.PinGridRowsGrow`）：寬度上限跟著框高等比放大
            //（owner 2026-10-18「多出來的高度給舞台、角色放大」）；設計格下＝原本的 285（`max-width: min(100%, 285px)`）。
            var tallScale = Mathf.Max(1f, boxHeight / DesignBoxHeight);
            var maxWidth = Mathf.Min(boxWidth, MaxPortraitWidth * tallScale);
            var maxHeight = Mathf.Max(1f, boxHeight - PortraitMargin);
            var scale = Mathf.Min(maxWidth / _texture.width, maxHeight / _texture.height);
            var drawn = new Vector2(_texture.width * scale, _texture.height * scale);

            var rect = (RectTransform)transform;
            rect.anchorMin = rect.anchorMax = new Vector2(0.5f, 0.5f);
            rect.pivot = new Vector2(0.5f, 0.5f);
            rect.anchoredPosition = Vector2.zero;
            if (tallScale > 1f)
            {
                // 長螢幕：真相是置中（`margin: auto`），但框一拉高，切平的胸像就懸在舞台中段、底下空一截；
                // 整個貼底又會上面空一大塊、名字牌蓋住身體（2026-10-18 兩種都實機看過）。⇒ 取中間：往下挪
                // 「貼底（保留設計格的下緣空隙）」那段位移的一半。
                var designGap = Mathf.Max(0f, (DesignBoxHeight - Mathf.Min(drawn.y, DesignBoxHeight)) * 0.5f);
                var toBottom = -(boxHeight - drawn.y) * 0.5f + designGap;
                rect.anchoredPosition = new Vector2(0f, toBottom * 0.5f);
            }
            rect.sizeDelta = drawn;

            // 驗收用的憑據：真機上 `adb logcat -s Unity` 直接讀實際數字
            // （貼圖 768x765 的話，這裡應該印出一個接近正方形的 drawn）。
            var stamp = $"{_character} {_texture.width}x{_texture.height} " +
                        $"{boxWidth:F0}x{boxHeight:F0} {drawn.x:F0}x{drawn.y:F0}";
            if (stamp != _logged)
            {
                _logged = stamp;
                Debug.Log($"[stage] 立繪 {_character} texture={_texture.width}x{_texture.height} " +
                          $"box={boxWidth:F0}x{boxHeight:F0} drawn={drawn.x:F0}x{drawn.y:F0}");
            }
            _fitted = true;
            _fitBox = box.rect.size;
            _image.enabled = true;
        }

        Vector2 _fitBox;

        Texture2D Load(string src)
        {
            if (string.IsNullOrEmpty(src) || string.IsNullOrEmpty(_character)) return null;
            var name = src;
            var dot = name.LastIndexOf('.');
            if (dot > 0) name = name.Substring(0, dot);
            var slash = name.LastIndexOf('/');
            if (slash >= 0) name = name.Substring(slash + 1);
            return Resources.Load<Texture2D>("characters/" + _character + "/" + name);
        }
    }
}
