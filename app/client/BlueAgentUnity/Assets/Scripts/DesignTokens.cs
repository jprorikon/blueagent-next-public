using UnityEngine;

namespace BlueAgent.Screens
{
    /// <summary>
    /// **設計數值＝從真相抄來的**（`blueagent/live2d/assets/webui/styles.css`），全 M3 只有這一份。
    ///
    /// 為什麼要單獨一支：M3 的驗收是「並排比對」，而我第一版是**估的**（字級 30、色票自己調）
    /// ——估的數值一比就歪，而且會散在每一屏裡各估一次。抄進這裡之後，數值只有一個來源，
    /// 每一條都註明 styles.css 的出處，要核對就回去看那一行。
    ///
    /// ⚠️ **座標系**：webui 的設計格是 `.app-shell { width: min(460px, 100%) }`
    /// ⇒ 數字是 **460 寬的 CSS px**。Unity 這邊就照這個座標系做畫布（`Reference`，460×818＝9:16），
    /// 再整體縮放到輸出圖 720×1280 ⇒ **比例與真相一致**（不是像素級相同，但並排比對要的是比例）。
    /// </summary>
    public static class DesignTokens
    {
        /// <summary>畫布設計格＝真相的檢視框（460 寬、9:16 ⇒ 818 高）。</summary>
        public static readonly Vector2 Reference = new Vector2(460f, 818f);

        // ── 色票（styles.css `:root`）────────────────────────────────────────────
        public static readonly Color Navy = Hex("2e4e75");
        public static readonly Color NavyDeep = Hex("183c66");
        public static readonly Color Sky = Hex("61d1f5");
        /// <summary>`.top-status i { background: #39c9e9; box-shadow: 0 0 0 3px #d7f4fb }`（6px 圓 ＋ 3px 外環）。</summary>
        public static readonly Color TopStatusDot = Hex("39c9e9");
        public static readonly Color TopStatusHalo = Hex("d7f4fb");
        public static readonly Color SkyLight = Hex("ccefff");
        public static readonly Color BlueSoft = Hex("eaf7ff");
        public static readonly Color Yellow = Hex("ffe43a");
        public static readonly Color Text = Hex("244365");
        public static readonly Color Line = Hex("cfdeeb");
        public static readonly Color White = Hex("ffffff");

        // ── 外殼 `.app-shell`（styles.css）────────────────────────────────────────
        /// <summary>外殼底色（線性漸層 165deg：#f6fbff → #eaf6ff 55% → #f7fbff）。</summary>
        public static readonly Color ShellTop = Hex("f6fbff");
        public static readonly Color ShellMid = Hex("eaf6ff");
        public static readonly Color ShellBottom = Hex("f7fbff");
        /// <summary>`.app-shell { box-shadow: inset 0 0 0 3px rgba(255,255,255,.8) }`——最外圈那 3px 白。</summary>
        public static readonly Color ShellRing = Rgba("ffffff", 0.80f);
        public const float ShellRingWidth = 3f;
        public const float ShellCorner = 12f;      // `.app-shell { border-radius: 12px }`
        /// <summary>
        /// `.app-shell { border: 1px solid #9cb9d0 }`——**最外圈那一條 1px 邊線**。
        ///
        /// ⚠️ 先前整條沒畫，而且少了它＝**內容整圈少內縮 1px**（border-box）⇒ 各屏都中：
        /// 實測症狀是「topbar 下緣比真相高 1px」「composer 上緣高 1px」「外殼最外那一列
        /// （y=0／y=817／x=0／x=459）對不上」（2026-10-12 逐行掃出來的）。
        /// </summary>
        public static readonly Color ShellEdge = Hex("9cb9d0");
        public const float ShellEdgeWidth = 1f;
        /// <summary>`.overlay-active .app-shell::after { background: rgba(23,47,74,.38) }`（開浮窗時那層暗）。</summary>
        public static readonly Color ShellDim = Rgba("172f4a", 0.38f);

        // ── 設定頁（`styles.css` 的 `.overlay` / `.settings-*` / `.setting-group`）──────
        public static readonly Color OverlayBg = Hex("0xf9fcff");
        public static readonly Color OverlayLine = Hex("0xa9c9dc");
        public static readonly Color OverlayHeadLine = Hex("0xbedae8");
        public static readonly Color OverlayCaption = Hex("0x80aac2");
        public static readonly Color TabBg = Hex("0xc8e9fa");
        public static readonly Color TabLine = Hex("0xadd3e8");
        public static readonly Color TabBorder = Hex("0xc8ddea");   // `.settings-tabs { border: 1px solid }`
        public static readonly Color TabText = Hex("0x246082");
        public static readonly Color TabSelectedText = Hex("0x133d62");
        public static readonly Color TabSelectedLine = Hex("0x65c7ed");
        public static readonly Color PageLine = Hex("0xd6e7f0");
        public static readonly Color HeadingAccent = Hex("0x63c8eb");
        public static readonly Color HeadingLine = Hex("0xc4dceb");
        public static readonly Color GroupLine = Hex("0xf0f3f5");
        public static readonly Color SettingLabel = Hex("0x42688a");
        public static readonly Color SettingHint = Hex("0x7b96ab");
        public static readonly Color HeadingSub = Hex("0x8caec2");
        public const float OverlayTop = 105f;      // `.overlay { inset: 105px 11px auto; height: 430px }`
        public const float OverlaySide = 11f;
        public const float OverlayHeight = 430f;
        public const float OverlayHeadHeight = 61f;
        // ── 設定面板抬頭 `.overlay-head`：115° 三段漸層 ＋ 標題（11px）＋ 標題下的黃短線 ───────
        // 全部照 `styles.css` 348–353；黃線與標題字級的數值另外用像素量過（2026-10-11）。
        public static readonly Color HeadGradA = Hex("0xd8f0fa");   // `linear-gradient(115deg, #d8f0fa 0 22%, …)`
        public static readonly Color HeadGradB = Hex("0xf9fcff");   // `…, #f9fcff 22% 78%, …`
        public static readonly Color HeadGradC = Hex("0xedf8fe");   // `…, #edf8fe 100%)`
        public const float HeadGradAngle = 115f;
        public const float HeadGradAt1 = 0.22f;     // 兩條**硬邊**的位置
        public const float HeadGradAt2 = 0.78f;
        /// <summary>標題**看得見的字**是 11px（`h2 span`）——實測真相墨跡 21×10。</summary>
        public const float HeadTitleSize = 11f;
        /// <summary>但 h2 自己的 `font-size: 18px` 撐起**行盒 strut**（`line-height: 1.25` ⇒ 22.5）。</summary>
        public const float HeadTitleBoxHeight = 22.5f;
        public const float HeadBarHeight = 4f;      // `h2::after { height: 4px }`
        public const float HeadBarGap = 2f;         // `margin-top: 2px`
        public const float HeadBarExtra = 4f;       // `h2 span { margin-left: 4px }` ⇒ 線寬＝文字寬＋4
        public const float HeadCaptionRatio = 1.125f; // 8px ⇒ 9（實測；不是一般文字的 1.33）
        /// <summary>小字那一行的行盒高（8px × 1.125 ＝ 9；`Settle` 量出來的就是這個值）。</summary>
        public const float HeadCaptionHeight = 9f;
        // ── `#service-fields h3`（服務頁的分組標題：`.settings-page h3` ＋ 這一條 id 規則）──────
        public static readonly Color GroupTitleText = Hex("0x246082");  // `#service-fields h3 { color }`
        public const float GroupTitleSize = 11f;    // `#service-fields h3 { font-size: 11px }`
        public const float GroupTitleTop = 12f;     // `margin: 12px 0 0`（＝上方留白、在盒子外）
        public const float GroupTitlePadBottom = 6f;// `.settings-page h3 { padding: 0 0 6px 6px }`
        public const float GroupTitleLine = 1f;     // `.settings-page h3 { border-bottom: 1px solid #c4dceb }`
        public const float GroupTitleInset = 6f;    // 上面那條的 `padding-left: 6px`
        public const float GroupTitleAccentWidth = 3f; // `.settings-page h3 { border-left: 3px solid #63c8eb }`
        /// <summary>盒高＝行盒＋6＋1＝21（實測：真相 h3 rect 高 21）；文字左緣 = 內容欄＋9。</summary>
        public const float GroupTitleBox = 21f;
        // ── `.service-save-row`（**貼在頁面可見區下緣**的儲存列；`position: sticky; bottom: 0`）──
        public const float SaveRowTop = 10f;        // `.setting-group` 的 padding-top
        public const float SaveRowHeight = 62f;     // 10＋13（標籤）＋6（gap）＋32（鈕）＋1
        public const float TabColWidth = 84f;      // `.settings-layout { grid-template-columns: 84px minmax(0,1fr) }`
        public const float TabHeight = 59f;
        public const float TabFontSize = 13f;
        public const float SettingsLayoutPad = 7f;
        public const float SettingsLayoutGap = 6f;
        public const float PagePad = 9f;           // `.settings-page { padding: 9px }`
        public const float GroupPadX = 5f;
        public const float GroupPadY = 10f;
        public const float GroupGap = 6f;
        public const float SettingLabelSize = 10f;
        public const float SettingHintSize = 9f;

        // ── `.settings-page h3`（頁頭；styles.css 361）＋ `.tech-button`（261／368）＋ select（370）──
        // 這三組都是 2026-10-11 深夜「A 動／B 外搬」那一輪補的：頁頭塊高度 ＝ **行盒 ＋ padding
        // ＋ border ＋ margin**（先前寫死 24，行高改行盒後就不夠了）、`.tech-button` 與 `<select>`
        // 的外框先前**整組沒搬**（音訊頁少了 3 個選單盒、對話／服務頁少了按鈕框）。
        public const float PageHeadingSize = 12f;    // `.settings-page h3 { font-size: 12px }`
        public const float PageHeadingPad = 6f;      // `padding-bottom: 6px`
        public const float PageHeadingLine = 1f;      // `border-bottom: 1px solid #c4dceb`
        public const float PageHeadingGap = 6f;      // `margin-bottom: 6px`
        public const float PageHeadingInset = 6f;    // `padding-left: 6px`（＝分組標題那條同一組數值）
        public const float PageHeadingAccent = 3f;   // `border-left: 3px solid #63c8eb`
        public const float TechButtonHeight = 32f;   // `.settings-page .tech-button { height: 32px }`
        public const float TechButtonRadius = 4f;    // `border-radius: 4px`
        public const float TechButtonTextSize = 11f; // `.settings-page .tech-button { font-size: 11px }`
        public const float TechButtonShadow = 2f;    // `box-shadow: 0 2px 0 #aac8d7`（＝下方 2px 實線）
        public const float TechButtonAngle = 125f;   // `linear-gradient(125deg, …)`（實測交界斜率 0.7＝tan35°）
        public const float TechButtonStop = 0.75f;   // `#f9fdff 0 75%, #d7f3fc 75%`
        public static readonly Color TechButtonLine = Hex("0x91bfd7");
        public static readonly Color TechButtonText = Hex("0x25587c");
        public static readonly Color TechButtonShadowColor = Hex("0xaac8d7");
        public static readonly Color TechButtonBgA = Hex("0xf9fdff");
        public static readonly Color TechButtonBgB = Hex("0xd7f3fc");
        // `#device-select, #input-language, #output-language, #ui-locale`（＝ `#service-fields select`
        // 同一組數值）：32 高、radius 3、1px #a5c5d7、底 #f8fcff、字 10px；箭頭是原生 select 的人字紋。
        public const float SelectHeight = 32f;
        public const float SelectRadius = 3f;
        public const float SelectTextSize = 10f;
        public const float SelectTextInset = 10f;    // 實測：墨跡從 135 起、盒左緣 125（CSS padding 5 ＋原生內縮）
        public const float SelectArrowInset = 9f;    // 人字紋中心：盒右緣往內 9px（實測 x 411..419／盒右 424）
        public static readonly Color SelectBg = Hex("0xf8fcff");
        public static readonly Color SelectLine = Hex("0xa5c5d7");
        public static readonly Color SelectArrow = Hex("0x183c66");
        // ── `<select>` **打開時那一張清單**（owner 2026-10-16：「照 webui 做成下拉框」）──────
        // webui 用的是**瀏覽器原生 `select`**：點開之後那張清單是**系統畫的**（白底、深字、每列
        // 等高、方角、帶陰影）⇒ 沒有真相可以逐像素對；這一組照那個樣子取（最不違和）。
        public const float SelectPopupRow = 30f;      // 每列高（原生選單一行大約這麼高）
        public const float SelectPopupGap = 2f;       // 清單離那一格盒子的距離
        public const float SelectPopupPad = 3f;       // 清單上下內緣
        public static readonly Color SelectPopupBg = Hex("0xffffff");
        public static readonly Color SelectPopupMark = Hex("0xe8f3fb");   // 選中那一列的底色
        // `<input type=range>`（`#output-volume { width: 100%; margin: 3px 0; accent-color: #00bce7 }`）：
        // 盒子 16 高（chromium 預設）＋上下 3px 外距；軌道 8 高／radius 4／1px `#858585`、
        // 未填 `#3b3b3b`、已填＝accent；拇指 ＝ **14 圓**，行程 ＝「軌道寬 − 直徑」（實測 80% ⇒
        // 圓心 x 360＝133＋0.8×284，兩者一致）。
        public const float RangeBox = 16f;
        public const float RangeMargin = 3f;
        public const float RangeTrackHeight = 8f;
        public const float RangeTrackRadius = 4f;
        public const float RangeThumb = 14f;
        public static readonly Color RangeTrack = Hex("0x3b3b3b");
        public static readonly Color RangeTrackLine = Hex("0x858585");
        public static readonly Color RangeFill = Hex("0x00bce7");
        // `.service-switch`（styles.css 479–482；服務頁那幾顆）——**與背景頁的 `.setting-switch` 不同數值**：
        // 26 高／radius 13／`min-width: 78px`／`padding: 0 9px 0 31px`（開＝左右對調）；滑塊 20 圓
        // **絕對定位** (2,2)、開時 `translateX(51px)`；字 11px（關 `#23537d`／開 `#1d6b3a`）。
        public const float ServiceSwitchWidth = 78f;
        public const float ServiceSwitchHeight = 26f;
        public const float ServiceSwitchRadius = 13f;
        public const float ServiceSwitchFontSize = 11f;
        public const float ServiceSwitchPad = 9f;         // padding 裡的那個 9（31 ＝ 2＋20＋9）
        public const float ServiceSwitchKnob = 20f;
        public const float ServiceSwitchKnobOffset = 2f;
        public const float ServiceSwitchKnobTravel = 51f;
        public static readonly Color ServiceSwitchOffBg = Hex("0xe7f6ff");
        public static readonly Color ServiceSwitchOffLine = Hex("0xbcd8ea");
        public static readonly Color ServiceSwitchOffText = Hex("0x23537d");
        public static readonly Color ServiceSwitchOffKnob = Hex("0x4f8fc9");
        public static readonly Color ServiceSwitchOnBg = Hex("0xe6f7ec");
        public static readonly Color ServiceSwitchOnLine = Hex("0x9fd8b4");
        public static readonly Color ServiceSwitchOnText = Hex("0x1d6b3a");
        public static readonly Color ServiceSwitchOnKnob = Hex("0x2eab63");

        // ── `.character-card`（styles.css 第 393–405 行；設定頁的角色／背景卡）──────────────
        // 68 高、padding 7、gap 9、radius 5、border 1px #b8d6e8 ＋ **border-left 4px #75c8eb**、
        // `box-shadow: 2px 2px 0 #d6e8f3`；選中＝bg #e6f7ff／border #1686bb／左緣 #00bce7。
        // 縮圖：背景卡 **72×42**（16:9 場景、`object-fit: cover`）、角色卡 **48×48**（`contain`）；
        // 兩者都是白底 ＋ 1px #c2deee 外框（縮圖由 `tools/sync_thumbs.sh`／`sync_avatars.sh` 烤好）。
        public const float CardHeight = 68f;         // min-height: 68px
        public const float CardPad = 7f;             // padding: 7px
        public const float CardAccentWidth = 4f;     // border-left: 4px
        public const float CardGap = 9f;             // gap: 9px
        public const float CardTitleSize = 12f;      // .character-card-copy strong
        public const float CardTagSize = 8f;         // .character-card-copy small
        public const float CardMarkSize = 9f;        // .character-card i
        public const float CardThumbW = 72f, CardThumbH = 42f;    // 背景卡縮圖
        public const float CardAvatarW = 48f, CardAvatarH = 48f;  // 角色卡縮圖
        // `.character-list { display: grid; gap: 8px; padding: 5px 1px 12px }`——**連續的卡自成一個列表容器**
        // （不是逐項直接排在頁面欄裡；真相那兩頁都是 `h3` ＋ hint ＋ `.setting-group` ＋ `.character-list`）。
        public const float CardListGap = 8f;         // gap: 8px（卡與卡的間距；不等於卡內 gap 9）
        public const float CardListPadX = 1f;        // padding 左右 1px
        public const float CardListPadTop = 5f;      // padding-top 5px
        public const float CardListPadBottom = 12f;  // padding-bottom 12px
        // ── 文字行高（2026-10-11 深夜，owner 拍板「A 動」）───────────────────────────────
        // 真相＝瀏覽器的行盒；Unity 那一側**行盒明顯偏短**（實測：一行提示 8 vs 真相 14、
        // 卡標題 11 vs 真相 16）⇒ 每一格都少幾 px、逐格累積（背景頁 −6、角色頁 −12）。
        // 原因：這一屏走 **legacy `Text` ＋ Hiragino Sans**（TMP 資產建不出來時的回退，見
        // `UiBuilder.SelectTextBackend`），而 Hiragino Sans 的自然行高比例 ≈ 0.92，
        // 瀏覽器那一側用的字型 ≈ 1.33（12px ⇒ 16、10px ⇒ 13）。
        // 修法：`UiBuilder.Text(..., lineRatio: …)` 把行高**乘到目標比例**（legacy 走 `lineSpacing`、
        // TMP 走 `lineSpacing × currentEmScale`）。用法：不給＝`TextLineRatio`（一般文字）；
        // CSS 明確寫了 `line-height` 的類別就照它的值傳（例：`.setting-hint { line-height: 1.55 }`）。
        public const float TextLineRatio = 1.33f;         // CSS `line-height: normal` 的實測等效值
        public const float SettingHintLineRatio = 1.55f;  // `.setting-hint { line-height: 1.55 }`

        // ── 字距（`letter-spacing`，單位＝em）──────────────────────────────────────────────
        // 為什麼要一組常數：真相那一側**幾乎每個標籤**都帶字距，而且不只影響外觀——它**計入寬度**
        // ⇒ 少了它所有全大寫標籤一律偏窄、連帶把同列後面的東西推歪（實測 `SYSTEM` 37 vs 57、
        // `MOMO` 39 vs 47、`STUDENT PROFILE 01` 也少一截）。值照 `styles.css` 逐條抄。
        public const float TrackBrand = 0.025f;   // `.brand strong { letter-spacing: .025em }`
        public const float TrackMid = 0.05f;      // `.status-prefix`／`.stage-serial`
        public const float TrackWide = 0.06f;     // `.speaker`／`.meter-cap`／`.asr-tag`／`.board-note`／`.chat-jump`
        public const float TrackWider = 0.08f;    // `.stage-index`／`.top-status`／`.heading-name small`
        public const float TrackCaption = 0.13f;  // `.profile-caption`
        public const float TrackSubtitle = 0.07f; // `.brand small`
        public const float TrackChip = 0.03f;     // `.mode-switch`
        /// <summary>`.momotalk-brand strong { font-size: 25px; letter-spacing: -.05em }`——**負的**（字要收緊）。</summary>
        public const float TrackBrandTight = -0.05f;
        public const float TrackOverlay = 0.11f;  // `.overlay-head small { letter-spacing: .11em }`
        public const float TrackTag = 0.04f;      // `#session-tag`
        public static readonly Color CardBg = Hex("0xf6fbfe");
        public static readonly Color CardSelBg = Hex("0xe6f7ff");
        public static readonly Color CardLine = Hex("0xb8d6e8");
        public static readonly Color CardSelLine = Hex("0x1686bb");
        public static readonly Color CardAccent = Hex("0x75c8eb");
        public static readonly Color CardSelAccent = Hex("0x00bce7");
        public static readonly Color CardThumbLine = Hex("0xc2deee");
        public static readonly Color CardTagText = Hex("0x5f8eaa");
        public static readonly Color CardMarkText = Hex("0x287bab");

        // ── `.setting-switch`（styles.css 第 470–473 行；真相是 index.html 的靜態元素 `#bg-random`）
        // 32 高膠囊（radius 16）＋ 20×20 圓滑塊＋狀態字（字典 `background.random.on/off`）＝ 開／關。
        // ⚠️ 這是**設定頁唯一一顆 `.setting-switch`**：服務頁那幾顆是 app.js 現生的 `.service-switch`
        //   （78 寬 × 26 高、radius 13、滑塊 `translateX(51px)`）——**另一支元件**，不要混用這裡的數值。
        public const float SwitchHeight = 32f;
        public const float SwitchRadius = 16f;
        public const float SwitchKnobSize = 20f;
        public const float SwitchGap = 8f;         // `.setting-switch { gap: 8px }`
        public const float SwitchPad = 9f;          // 1px 邊框 ＋ `padding: 0 8px`（膠囊內那一列的左右留白）
        public const float SwitchFontSize = 11f;
        public static readonly Color SwitchOnBg = Hex("0xe4f8ec");      // `.setting-switch.is-on { background }`
        public static readonly Color SwitchOnLine = Hex("0x96d8b4");    // `.setting-switch.is-on { border-color }`
        public static readonly Color SwitchOnKnob = Hex("0x28a35f");    // `.is-on .setting-switch-knob { background }`
        public static readonly Color SwitchOnText = Hex("0x0d5c36");    // `.setting-switch.is-on { color }`
        public static readonly Color SwitchOffBg = Hex("0xe7f6ff");     // `.setting-switch { background }`
        public static readonly Color SwitchOffLine = Hex("0xbcd8ea");   // `.setting-switch { border }`
        public static readonly Color SwitchOffKnob = Hex("0x4f8fc9");   // `.setting-switch-knob { background }`
        public static readonly Color SwitchOffText = Hex("0x23537d");   // `.setting-switch { color }`

        // ── `.topbar` ────────────────────────────────────────────────────────────
        public const float TopbarHeight = 55f;      // height: 55px
        public const float TopbarGap = 10f;         // gap: 10px
        public const float TopbarPadLeft = 10f;     // padding: 0 12px 0 10px
        public const float TopbarPadRight = 12f;
        public static readonly Color TopbarLine = Hex("90d5f1");        // border-bottom 2px
        public static readonly Color TopbarTint = Hex("ebf8ff");        // 漸層左段
        public static readonly Color TopbarTint2 = Hex("eff9ff");       // 漸層右段

        // `.brand strong` 17px／800／ls .025em；`.brand strong span` 13px；`.brand small` 8px #6b96b5
        public const float BrandSize = 17f;
        public const float BrandShortSize = 13f;
        public const float BrandCaptionSize = 8f;
        public static readonly Color BrandCaption = Hex("6b96b5");

        // `.mode-switch` min-width 66、height 24、radius 12、font 10/800；
        //   is-work：bg #fff0d8、color #7a4508、border #e8c98e
        public const float ChipMinWidth = 66f;
        public const float ChipHeight = 24f;
        public const float ChipRadius = 12f;
        public const float ChipFontSize = 10f;
        // ── `.nav-button`（topbar 左上那顆 ☰；owner 2026-10-12 像素複核：整顆沒畫）────────────
        //    `{ width/height: 39px; border-radius: 50%; background: #496a9d }` ＋ 三條 2.5px 白條
        //    （`span` ＋ `::before`／`::after`）。少了它**品牌整組左移 50px**（真相 `ASTER` 墨跡 x=60）。
        public const float NavSize = 39f;
        public static readonly Color NavBg = Hex("496a9d");
        public const float NavBarWidth = 17f;
        public const float NavBarHeight = 2.5f;
        public const float NavBarStep = 5.25f;
        // ── `.mode-switch` 的滑塊（`i.mode-switch-knob { 18×18 圓、left/top 2 }`；is-work 平移 42）──
        public const float ModeKnobSize = 18f;
        public const float ModeKnobInset = 2f;
        public const float ModeKnobStep = 42f;
        public const float ModeKnobRadius = 9f;
        public static readonly Color ModeKnobBg = Hex("4f8fc9");
        public static readonly Color ModeKnobWorkBg = Hex("e29b26");
        /// <summary>`.mode-switch { padding: 0 6px 0 27px }`（is-work 反過來）——文字要讓開那顆滑塊。</summary>
        public const float ModePadLeft = 27f;
        public const float ModePadRight = 6f;
        public static readonly Color ChipBg = Hex("e7f6ff");
        public static readonly Color ChipText = Hex("23537d");
        public static readonly Color ChipBorder = Hex("bcd8ea");
        public static readonly Color ChipWorkBg = Hex("fff0d8");
        public static readonly Color ChipWorkText = Hex("7a4508");

        // ── `.main-content`：padding 8px 10px 10px、gap 7px ───────────────────────
        public const float ContentPadTop = 8f;
        public const float ContentPadSide = 10f;
        public const float ContentPadBottom = 10f;
        public const float ContentGap = 7f;
        /// <summary>
        /// 真相那一頁的 **1px 外框**（`body` 的內容框內縮 1px）在 grid 算列高時要扣掉幾 px。
        ///
        /// ⚠️ **2026-10-12 改成 0**：先前是 1（＝扣 2px），因為那時 **Unity 根本沒畫那個框**
        /// （補償式做法）。這一輪把框做成**真的**（畫布根節點 `padding = 1`，見 `UiBuilder.NewCanvas`）
        /// ⇒ 再扣一次就是**算兩次**：實測症狀＝舞台那一列被壓成 382（真相 384）、
        /// 卡與 composer 都高 2px（逐行掃：卡上緣 453 vs 真相 455、composer 上緣 659 vs 661）。
        /// 補償層要記得拆——同 `references/box-shadow-and-shell-layers.md` §6「補救層」那條。
        /// </summary>
        public const float PageFrameInset = 0f;
        public const float StageMin = 180f;         // minmax(180px, 1fr)
        public const float ConverationMin = 96f;    // minmax(96px, .52fr)
        public const float ConversationWeight = 0.52f;
        public const float StatusHeight = 18f;      // 18px

        // ── `.speech-bubble`：padding 5px 9px、border-left 3px #60cdef、底 #f4fbff→#fff；
        //    `p` 11px／1.35／#2e4a67
        /// <summary>
        /// `.speech-bubble { padding: 5px 9px; border-left: 3px solid #60cdef }`——**左邊要 12**：
        /// 那 3px 是**邊框**（border-box 之下吃掉內容寬），UGUI 這邊是畫一條疊層的線、不吃內距
        /// ⇒ 折進內距（少了它氣泡裡的字整組左移 4px，實測真相 `ASTER` x=33、我這邊 29）。
        /// </summary>
        public const float BubblePadX = 12f;
        public const float BubblePadY = 5f;
        public const float BubbleFontSize = 11f;
        public const float BubbleTagSize = 8f;
        public static readonly Color BubbleLine = Hex("60cdef");
        public static readonly Color BubbleTop = Hex("f4fbff");
        public static readonly Color BubbleText = Hex("2e4a67");

        // ── `.composer`：gap 6、padding 5、border 1px #afcfdf、radius 5、白底 ──────
        public const float ComposerGap = 6f;
        public const float ComposerPad = 5f;
        public const float ComposerRadius = 5f;
        public static readonly Color ComposerBorder = Hex("afcfdf");

        // `.asr-preview` height 66、padding 9px 10px 0、border #cfe0ea、radius 4、bg #f6fbfe、12px
        public const float AsrHeight = 66f;
        /// <summary>`#asr-text { line-height: 1.35 }` / `.asr-tag { line-height: 16px; padding: 0 5px; background #dceefb }`。</summary>
        public const float AsrTagLine = 16f;
        public static readonly Color AsrTagBg = Hex("dceefb");
        public static readonly Color AsrTagText = Hex("35688d");
        /// <summary>`.asr-preview`／`.meter-vbox` 的 `border: 1px solid #cfe0ea`、radius 4。</summary>
        public static readonly Color SoftBoxBorder = Hex("cfe0ea");
        public const float SoftBoxRadius = 4f;
        // ── `.meter-vbox`（收音電平表；`flex: 0 0 30px`、padding 3、gap 3、bg #f6fbfe）───────────
        public const float MeterWidth = 30f;
        public const float MeterPad = 3f;
        public const float MeterGap = 3f;
        public static readonly Color MeterBg = Hex("f6fbfe");
        /// <summary>`.mic-meter-track { border: 1px solid #b9d7e7; background: #eef7fc }`（那條空的表）。</summary>
        public static readonly Color MeterTrackBg = Hex("eef7fc");
        public static readonly Color MeterTrackBorder = Hex("b9d7e7");
        /// <summary>`.mic-meter-peak { height: 2px; left/right: -3px; background: #456a88 }`——沒訊號時它就是底部那一條。</summary>
        public static readonly Color MeterPeak = Hex("456a88");
        /// <summary>`.meter-cap { color: #4b6a83; font-size: 8px }`（＝投影的 `MeterCap`／`MIC`）。</summary>
        public static readonly Color MeterCapText = Hex("4b6a83");
        public const float AsrFontSize = 12f;
        public static readonly Color AsrBg = Hex("f6fbfe");
        public static readonly Color AsrText = Hex("4b6a83");

        // `.mic-button` height 29、font 10/800、bg #eef8fd、color #386685；is-on ⇒ #bdeeff
        //   `.mic-button.is-primary` height 37、font 12、底 #b9ecff→#63cbee
        public const float MicHeight = 29f;
        public const float MicFontSize = 10f;
        public const float MicPrimaryHeight = 37f;
        public static readonly Color MicBg = Hex("eef8fd");
        public static readonly Color MicText = Hex("386685");
        public static readonly Color MicOnBg = Hex("bdeeff");
        // ── `.mic-button.is-primary`（主頁那一顆主鈕；owner 2026-10-12 像素複核：樣式整組沒搬）──────
        //    `{ justify-content: center; height: 37px; border-color: #55b8df; color: #14405f;
        //      background: linear-gradient(125deg, #b9ecff 0 76%, #63cbee 76%);
        //      box-shadow: 0 2px 0 #8fc6dd; font-size: 12px; border-radius: 3px }`
        public static readonly Color MicPrimaryBorder = Hex("55b8df");
        public static readonly Color MicPrimaryGradA = Hex("b9ecff");
        public static readonly Color MicPrimaryGradB = Hex("63cbee");
        public const float MicPrimaryStop = 0.76f;    // 漸層硬邊的位置
        public static readonly Color MicPrimaryShadow = Hex("8fc6dd");
        public static readonly Color MicPrimaryText = Hex("14405f");
        public const float MicPrimaryFontSize = 12f;
        public const float MicPrimaryRadius = 3f;
        /// <summary>`.mic-light { width: 7px; height: 7px; border-radius: 50% }`（`is-on` ⇒ `#159bd2`）。</summary>
        public const float MicDotSize = 7f;
        public static readonly Color MicDotOff = Hex("9ab5c6");
        public static readonly Color MicDotOn = Hex("159bd2");
        // ── `.chat-jump`（MOMO 那一顆；粉色漸層）────────────────────────────────────────────
        //    `{ flex: 0 0 25%; height: 37px; padding: 0 4px; gap: 5px; border: 1px solid #d47391;
        //      border-radius: 4px 4px 4px 2px; color: white; background: linear-gradient(#ffa2b7, #ee8ca6);
        //      box-shadow: 0 2px 0 #d47391; font-size: 12px; letter-spacing: .06em }`
        public static readonly Color JumpGradA = Hex("ffa2b7");
        public static readonly Color JumpGradB = Hex("ee8ca6");
        public static readonly Color JumpBorder = Hex("d47391");
        public static readonly Color JumpShadow = Hex("d47391");
        public const float JumpRadius = 4f;           // 左下角真相是 2px（`4px 4px 4px 2px`）
        public const float JumpCorner = 2f;
        public const float JumpFontSize = 12f;
        /// <summary>`.chat-jump-mark { width/height: 17px; border-radius: 50% 50% 50% 4px; background: white;
        /// color: #ee8ca6; transform: rotate(-12deg) }`——白色對話泡泡，裡面是粉色的棋子。</summary>
        public const float JumpMarkSize = 17f;
        public static readonly Color JumpMarkIcon = Hex("ee8ca6");

        // ── `.status-line`：font 9、color #6588a1、gap 6；`.status-prefix` #288abb／800 ──
        public const float StatusFontSize = 9f;
        public static readonly Color StatusText = Hex("6588a1");
        public static readonly Color StatusPrefix = Hex("288abb");

        // ── 舞台上的名牌 `.profile-tag`：left 10／bottom 9、寬 59%（上限 242）、
        //    padding 7px 12px 8px、底 rgba(33,65,108,.91)、skewX(-9deg)、黃底線 75%×3px ──
        public const float ProfileTagLeft = 10f;
        public const float ProfileTagBottom = 9f;
        /// <summary>`.profile-tag { width: 59%; max-width: 242px }`——59% 要**再被 242 夾住**（見 `ShellView`）。</summary>
        public const float ProfileTagWidthRatio = 0.59f;
        public const float ProfileTagMaxWidth = 242f;
        /// <summary>`.profile-tag` 的 border box 高＝7(pad) ＋ 37(內容：caption 9＋名字行 19＋6＋底線 3) ＋ 8(pad)。</summary>
        public const float ProfileTagHeight = 52f;
        public const float ProfileNameSize = 16f;
        public const float ProfileCaptionSize = 8f;
        public const float ProfileUnderline = 3f;
        public static readonly Color ProfileBg = new Color(33f / 255f, 65f / 255f, 108f / 255f, 0.91f);
        public static readonly Color ProfileCaption = Hex("aeeaff");

        // `.stage-index` 9px／800／#356085（左上、左緣 3px 藍線）；`.stage-serial` 8px／#5b9abd（右上）
        public const float StageIndexSize = 9f;
        public const float StageSerialSize = 8f;
        public static readonly Color StageIndexText = Hex("356085");
        public static readonly Color StageSerialText = Hex("5b9abd");
        /// <summary>`.character-stage { border: 1px solid #c4e0ed; border-radius: 5px }`。</summary>
        public static readonly Color StageBorder = Hex("c4e0ed");
        public const float StageRadius = 5f;
        /// <summary>`.state-chip { font-size: 9px; font-weight: 800 }`（標籤）＋ `i { 7×7 圓、#46c4e5 }`。</summary>
        public const float StateChipFontSize = 9f;
        public const float StateLightSize = 7f;
        public static readonly Color StateLight = Hex("46c4e5");
        /// <summary>`.state-chip { border: 1px solid #b8d6e8; border-radius: 4px }`（與 topbar 那顆 `.mode-switch` 不同色）。</summary>
        public static readonly Color StateChipBorder = Hex("b8d6e8");
        public const float StateChipRadius = 4f;

        // ── `box-shadow` 那一族（照 `styles.css` 逐條抄；全走 `UiBuilder.ShadowBehind`）─────────
        // ⚠️ UGUI 的 **y 要變號**（CSS 的 +y 往下、Unity 的 +y 往上）⇒ 呼叫端一律傳 `-dy`。
        // 命名照 CSS 的選擇器取，回頭對得上；alpha 走 `Rgba`（`Hex` 只吃 6 位）。
        public static readonly Color ShadowConv = Rgba("5184aa", 0.16f);      // .conversation／.game-talk／.game-board／.trpg-*：`0 3px 0`
        public static readonly Color ShadowComposer = Rgba("5184aa", 0.14f);  // .composer：`0 3px 0`
        public static readonly Color ShadowTopbar = Rgba("31608c", 0.12f);    // .topbar：`0 3px 10px`
        public static readonly Color ShadowNav = Rgba("26446c", 0.28f);       // .nav-button：`0 3px 5px`
        public static readonly Color ShadowChip = Rgba("254a6d", 0.18f);      // .state-chip：`0 2px 3px`
        public static readonly Color ShadowKnob = Rgba("204066", 0.34f);      // .mode-/setting-/service-switch-knob：`0 1px 2px`
        public static readonly Color ShadowOverlay = Rgba("275077", 0.26f);   // .overlay：`0 5px 14px`
        public static readonly Color ShadowHistory = Rgba("1c2c42", 0.28f);   // .history-overlay：`-7px 5px 16px`
        public static readonly Color ShadowMenuCard = Rgba("17304c", 0.20f);  // .menu-card：`0 5px 0`
        public static readonly Color ShadowTile = Hex("89929b");              // .menu-tile／.menu-back：`0 3px 0`／`0 2px 0`
        public static readonly Color ShadowTech = Hex("aac8d7");              // .tech-button／.send-button：`0 2px 0`
        public static readonly Color ShadowQuestion = Rgba("1b3a58", 0.25f);  // .question-card：`0 5px 0`
        public static readonly Color ShadowProfile = Rgba("ffffff", 0.70f);   // .profile-tag：`3px 3px 0`
        public static readonly Color ShadowStone = Rgba("143250", 0.35f);     // .gomoku .stone：`0 1px 1px`

        /// <summary>`rgba(...)` 那一種（`Hex` 只吃 `#rrggbb`，alpha 一律 1）。</summary>
        private static Color Rgba(string hex, float alpha)
        {
            var color = Hex(hex);
            color.a = alpha;
            return color;
        }

        /// <summary>把 `#rrggbb` 轉成 Color（抄色票時少打字、也少打錯）。</summary>
        /// <summary>給**別的屏**抄真相色票用的公開入口（`Hex` 本身是 private）。
        /// ⚠️ 只是入口、不是第二份來源：長期仍以本檔的具名 token 為準。</summary>
        public static Color HexLocal(string hex) => Hex(hex);

        private static Color Hex(string hex)
        {
            // 容忍 `0x`／`#` 前綴——**這一行不是潔癖**：`Hex` 是在**靜態初始化**裡被呼叫的，
            // 只要有一個值格式不對，整個 `DesignTokens` 型別就炸（TypeInitializationException），
            // 症狀是「畫面全空、每一幀拋錯、無頭流程卡住不出圖」，而 log 裡看不到是哪個值壞掉。
            hex = hex.Trim().TrimStart('#');
            if (hex.StartsWith("0x") || hex.StartsWith("0X")) hex = hex.Substring(2);
            var r = System.Convert.ToInt32(hex.Substring(0, 2), 16) / 255f;
            var g = System.Convert.ToInt32(hex.Substring(2, 2), 16) / 255f;
            var b = System.Convert.ToInt32(hex.Substring(4, 2), 16) / 255f;
            return new Color(r, g, b, 1f);
        }
    }
}
