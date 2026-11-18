using BlueAgent.Screens;
using System;
using System.Collections;
using System.Collections.Generic;
using BlueAgent.Logic;
using BlueAgent.Spike;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>
    /// **blueagent-next 的端末殼**：開機 → 讀 token → `GET /api/me` → 依 `role` 進 master 端或 guest 端。
    ///
    /// 契約：倉庫根目录 `docs/accounts-and-clients.md`（§2 啟動流程、§4 錯誤語義）。
    /// 兩端的差別只有三處（照契約）：① master 多「アカウント」分頁；② guest 只看到自己綁定的角色；
    /// ③ guest `want_voice=false`（伺服器一律不出聲）。其餘畫面是同一份。
    ///
    /// 這個場景**不含**舊橋的 `AppShell`／`BridgeShell`（另一個 `.unity`），所以舊包的行為不受影響。
    /// </summary>
    public sealed class NextShell : MonoBehaviour
    {
        public static NextShell Instance { get; private set; }

        public NextApi Api { get; private set; }
        public SpeechPlayer Voice { get; private set; }
        /// <summary>語音簡訊的錄音器（B1）。</summary>
        public NextVoice VoiceRecorder { get; private set; }

        /// <summary>聊天頁上方的立繪（B2，兩端都有）。分頁切走時會被重建。</summary>
        public StageAvatar Stage { get; set; }

        public JObject Me { get; private set; }
        /// <summary>這一端看得到的角色清單（`/api/me` 的 entities）。多於一個時聊天頁會出現切換藥丸。</summary>
        public JArray Entities { get; private set; }
        public bool IsMaster { get { return Me != null && Me.OptString("role") == "master"; } }
        public string Principal { get { return Me == null ? "" : Me.OptString("principal"); } }
        /// <summary>S3 會話與訊息作者只認 canonical account id；舊 /api 個人資料仍使用 Principal。</summary>
        public string ChatAccountId { get { return Me == null ? "" : Me.OptString("account_id", Principal); } }
        public string EntityId { get; private set; } = "";
        /// <summary>這個角色的**後台名**（伺服器的 `name`）。畫面上顯示用 <see cref="EntityDisplayName"/>。</summary>
        public string EntityName { get; private set; } = "";
        /// <summary>最近一輪的 thread（`/api/state`／`result` 回報；狀態列用）。</summary>
        public string LastThread { get; private set; } = "";

        /// <summary>master 端的分頁（`chat`／`accounts`／`status`）。</summary>
        public string MasterTab = "chat";

        /// <summary>guest 端現在是不是開著設定頁（`設定` 從聊天抬頭那顆鈕進來）。</summary>
        public bool GuestSettingsOpen { get; private set; }

        private Camera _camera;
        private Canvas _canvas;
        private UnityEngine.UI.VerticalLayoutGroup _rootGroup;
        private RectTransform _topbar;
        /// <summary>設定內部頁籤（audio／mood／bg／service／diag）。</summary>
        public string SettingsPage { get; private set; } = "audio";
        public bool MoodSettingBusy { get; private set; }
        public string MoodSettingMessage { get; private set; } = "";
        public bool MoodSettingError { get; private set; }
        private bool _moodSettingsLoading;
        /// <summary>`/api/health` 的回包（服務頁顯示引擎與件數用）。</summary>
        public JObject HealthCounts { get; private set; }
        private UnityEngine.UI.Button _settingsButton;
        /// <summary>頂欄的聊天入口（只在公共区亮）＋未讀數（設計稿 v4 ⑦：一級頁沒有輸入欄，她說話要看得到）。</summary>
        private UnityEngine.UI.Button _chatButton;
        private RectTransform _chatBadge;
        private Component _chatBadgeText;
        private RectTransform _body;
        /// <summary>`_body` 的寬度約束：大螢幕上聊天以外的頁面置中、限寬（見 <see cref="CapBody"/>）。</summary>
        private UnityEngine.UI.LayoutElement _bodyLayout;
        /// <summary>
        /// 現在這一頁怎麼重畫（螢幕大小變了：Duo 開合、轉向）。換頁一律先清掉（`ClearBody`），
        /// 能安全重畫的頁才設；通話／棋局／桌面重畫會重撥／重開局 ⇒ 不設，只重排版面。
        /// </summary>
        private Action _rebuild;
        private Vector2Int _screenSize;
        private float _relayoutAt;
        /// <summary>螢幕變了之後什麼時候決定橫向要不要收（給系統時間把縮著的 app 放大，見 `NextLayout.AllowLandscape`）。</summary>
        private float _orientAt;
        /// <summary>剛剛是自己把橫向收掉（轉回直向）⇒ 下一次尺寸變化不再試探，免得開／收來回翻。</summary>
        private bool _quietNextChange;
        /// <summary>body 外面那一層（一次性提示浮在它的上緣，見 `NextToast`）。</summary>
        private RectTransform _bodyHost;
        private RectTransform _connBanner;
        private Component _connBannerText;
        private NextChatView _chat;
        private NextChatListView _chatList;
        private NextFeedView _feed;
        private NextComposeView _compose;
        private NextPostView _post;
        private NextProfileView _profile;
        /// <summary>目前這一頁怎麼重回（個人主頁的「返回」用）；公共区／詳情／主頁各自設。</summary>
        private UnityEngine.Events.UnityAction _here;
        /// <summary>帖子詳情的「返回」：從哪一頁點進來就回哪一頁（公共区或個人主頁）。</summary>
        public UnityEngine.Events.UnityAction PostBack { get; private set; }
        private bool _pickForCompose;
        private bool _loggedIn;
        private float _keyboardDesign;

        private readonly Queue<string> _speechQueue = new Queue<string>();
        private Coroutine _speechPump;
        /// <summary>位置共享以服务端为准；本地仅缓存已确认状态，默认关闭。</summary>
        public bool LocationSharing { get { return NextConfig.LocationSharing; } }
        public bool LocationSharingBusy { get; private set; }
        public string LocationSharingMessage { get; private set; } = "";
        public bool LocationSharingError { get; private set; }
        private bool _locationStatusLoading;
        /// <summary>位置推送迴圈：登录并确认授权后才开、登出或撤销就停。</summary>
        private Coroutine _locationRoutine;

        private void Awake()
        {
            Instance = this;
            Api = gameObject.AddComponent<NextApi>();
            // 任何登入中的請求 401 ⇒ 這裡收口一次（U-6；以前要每個頁面自己記得叫 HandleAuth）
            Api.Unauthorized += OnTokenInvalid;
            Api.ChatSync.Accepted += (conversationId, mutationId) => _chat?.OnChatAccepted(conversationId, mutationId);
            Voice = gameObject.AddComponent<SpeechPlayer>();
            // 3D 立繪的嘴照她的語音音量開合（沒在播 ⇒ -1，舞台退回固定節奏）
            NextStage3D.SpeechLevel = () => Voice != null && Voice.IsPlaying ? (float)Voice.LastLevel : -1f;
            Voice.Volume = NextConfig.Volume;
            VoiceRecorder = gameObject.AddComponent<NextVoice>();
            _camera = GetComponent<Camera>();
            if (_camera == null) _camera = Camera.main;
            // 常駐推播要活著（她主動說話、後續交付都走那條線）
            Application.runInBackground = true;
        }

        /// <summary>要不要她出聲：guest 一律 false（契約 §1 能力表）。</summary>
        public bool WantVoice { get { return IsMaster && NextConfig.WantVoice; } }

        private void Start()
        {
            NextLayout.ApplyOrientation();         // 大螢幕（Duo 展開屏）才放開橫向
            _screenSize = new Vector2Int(Screen.width, Screen.height);
            BuildChrome();
            Boot();
            // 配對深連結（多設備，2026-11-16）：冷啟動帶進來的在 absoluteURL；開著時來的走事件
            Application.deepLinkActivated += OnDeepLink;
            if (!string.IsNullOrEmpty(Application.absoluteURL)) OnDeepLink(Application.absoluteURL);
            // 前景服務＋常駐通知（P7-③）：app 退到背景也收得到她主動說的話
            try { NativeShell.StartShellService(NextStrings.Get("thinking")); }
            catch (Exception error) { Debug.LogWarning("shell service: " + error.Message); }
            // 位置推送不在這裡開：未登录、未确认授权不该要权限或读取座標。
        }

        /// <summary>只有服务端确认开启后才请求系统权限；失败一律关闭运行时传感器。</summary>
        private void StartLocation()
        {
            StopLocation();
            if (!_loggedIn || !NextConfig.LocationSharing) return;
            var epoch = Api.Session.Epoch;
            try
            {
                _locationRoutine = StartCoroutine(NextLocation.Loop(Api,
                    state => Debug.Log("[next-location] " + state),
                    () => _loggedIn && NextConfig.LocationSharing && Api.Session.IsLive(epoch)));
            }
            catch (Exception error) { Debug.LogWarning("location: " + error.Message); }
        }

        private void StopLocation()
        {
            if (_locationRoutine != null) StopCoroutine(_locationRoutine);
            _locationRoutine = null;
            NextLocation.Stop();                 // StopCoroutine 不执行迭代器末尾，必须显式停传感器。
            Api.CancelLocation();               // 已发出的那一发也不能带着旧身份继续（U-3）
        }

        public void RefreshLocationSharing()
        {
            if (!_loggedIn || _locationStatusLoading || LocationSharingBusy) return;
            _locationStatusLoading = true;
            Api.Get("/v1/me/location", reply =>
            {
                _locationStatusLoading = false;
                if (!reply.Ok)
                {
                    NextConfig.LocationSharing = false;
                    StopLocation();
                    LocationSharingError = true;
                    LocationSharingMessage = NextStrings.F("read_fail", "reason", reply.UserMessage());
                }
                else
                {
                    var sharing = reply.Json?.OptJObject("sharing");
                    NextConfig.LocationSharing = sharing != null && sharing.OptBoolean("enabled");
                    LocationSharingError = false;
                    LocationSharingMessage = "";
                    if (LocationSharing) StartLocation(); else StopLocation();
                }
                if (_loggedIn && GuestSettingsOpen && SettingsPage == "service") RenderSettings();
            });
        }

        /// <summary>PUT 成功且回包状态一致才更新本地开关；撤销失败不得假装已经停止。</summary>
        public void SetLocationSharing(bool enabled)
        {
            if (!_loggedIn || LocationSharingBusy) return;
            LocationSharingBusy = true;
            LocationSharingError = false;
            LocationSharingMessage = NextStrings.Get("location_saving");
            RenderSettings();
            Api.Put("/v1/me/location", new JObject().Put("enabled", enabled), reply =>
            {
                LocationSharingBusy = false;
                var sharing = reply.Json?.OptJObject("sharing");
                var confirmed = reply.Ok && sharing != null && sharing.OptBoolean("enabled") == enabled;
                if (!confirmed)
                {
                    LocationSharingError = true;
                    LocationSharingMessage = NextStrings.F("location_save_fail", "reason", reply.UserMessage());
                }
                else
                {
                    NextConfig.LocationSharing = enabled;
                    LocationSharingMessage = NextStrings.Get("location_saved");
                    if (enabled) StartLocation(); else StopLocation();
                }
                if (_loggedIn && GuestSettingsOpen && SettingsPage == "service") RenderSettings();
            });
        }

        // ── 殼的骨架（topbar / body / status）────────────────────────────────────

        private void BuildChrome()
        {
            if (_canvas != null) Destroy(_canvas.gameObject);
            _canvas = NextUi.NewPage(_camera, "NextCanvas");
            var root = (RectTransform)_canvas.transform;
            NextUi.Background(root);
            _rootGroup = root.GetComponent<UnityEngine.UI.VerticalLayoutGroup>();
            _edge = _rootGroup.padding.left;
            _keyboardDesign = 0f;

            // 頂欄：左＝品牌（粗斜體＋天藍斜條「NEXT」），右＝身份徽章、連線狀態、設定鈕
            _topbar = UiBuilder.Row("Topbar", root, 8f, new RectOffset(TopbarPadLeft, TopbarPadRight, 0, 0), TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(_topbar, 52f);
            var bar = UiBuilder.Backdrop("TopbarBg", _topbar, new Color(1f, 1f, 1f, 0.82f));
            var drop = bar.gameObject.AddComponent<UnityEngine.UI.Shadow>();
            drop.effectColor = NextTheme.Shadow;
            drop.effectDistance = new Vector2(0f, -2f);
            var brand = UiBuilder.Text("TopBrand", _topbar, "BlueAgent", 17f, NextTheme.Ink,
                                       TextAlignmentOptions.MidlineLeft, noWrap: true, bold: true);
            NextUi.Italic(brand);
            NextUi.Badge("TopNext", _topbar, "NEXT", NextTheme.Sky, NextTheme.OnColor);
            var spacer = UiBuilder.NewRect("TopSpacer", _topbar);
            UiBuilder.Weight(spacer, 1f);
            _roleBadge = NextUi.Badge("TopRole", _topbar, "", NextTheme.SkyPale, NextTheme.SkyDeep);
            _roleBadge.gameObject.SetActive(false);
            _connBadge = NextUi.Badge("TopConn", _topbar, NextStrings.Get("badge_offline"), NextTheme.IdlePale,
                                      NextTheme.InkSoft, NextTheme.Idle);
            // 首頁右上角＝設定（owner 2026-10-22：不再要 ☰ menu，也不要「對話紀錄／新對話」）
            _chatButton = NextUi.IconButton("TopChat", _topbar, "chat", NextUi.Style.Secondary, 38f);
            _chatButton.onClick.AddListener(ShowChatList);
            BuildChatBadge();
            _chatButton.gameObject.SetActive(false);          // 登入後才顯示
            _settingsButton = NextUi.IconButton("TopSettings", _topbar, "settings", NextUi.Style.Secondary, 38f);
            _settingsButton.onClick.AddListener(ShowSettings);
            _settingsButton.gameObject.SetActive(false);      // 登入後才顯示

            // 斷線橫幅（owner 2026-10-30）：以前斷線只有頂欄徽章與底部狀態列的小字在變，很難注意到。
            // 連線掉了就在頂欄下面掛一條（`SetConnBadge` 控制顯示），連回來就收。
            _connBanner = UiBuilder.Row("ConnBanner", root, 6f, new RectOffset(14, 14, 0, 0), TextAnchor.MiddleCenter);
            UiBuilder.FixedHeight(_connBanner, 28f);
            UiBuilder.Backdrop("ConnBannerBg", _connBanner, NextTheme.WarnPale);
            UiBuilder.Icon("ConnBannerIcon", "pulse", _connBanner, 13f, NextTheme.Warn);
            _connBannerText = UiBuilder.Text("ConnBannerText", _connBanner, NextStrings.Get("conn_banner"),
                                             NextTheme.NoteSize, NextTheme.Warn, TextAlignmentOptions.Center,
                                             noWrap: true, bold: true);
            _connBanner.gameObject.SetActive(false);

            // body 外面包一層橫排：大螢幕上設定／相冊這些頁置中限寬，聊天頁照樣佔滿（`CapBody`）
            _bodyHost = UiBuilder.Row("BodyHost", root, 0f, null, TextAnchor.UpperCenter);
            UiBuilder.Weight(_bodyHost, 1f);
            _body = UiBuilder.Column("Body", _bodyHost, 0f, null, true, TextAnchor.UpperLeft);
            _bodyLayout = UiBuilder.Weight(_body, 1f);
            CapBody(false);

            // ⚠️ 底部狀態列拿掉了（owner 2026-10-30「完全可以不顯示，下方空間更乾淨、輸入框整體下移」）：
            // 身份與連線狀態頂欄的徽章已經有；thread 在設定 › 診斷看；一次性的提示改成浮出的小膠囊（`Status`）。
            ApplyRootPadding();                    // 安全區（頂欄要先建好：直向時左右內縮只給它）
        }

        /// <summary>頂欄兩顆徽章現在顯示的那一條（key）：換界面語言時照原樣重套（`ApplyLanguage`）。</summary>
        private string _connKey = "badge_offline";
        private string _roleKey = "";
        private RectTransform _connBadge;
        private RectTransform _roleBadge;

        /// <summary>連線狀態徽章：顏色跟狀態走（連上＝綠點會呼吸、重連＝橘、離線／連線中＝灰）。</summary>
        private void SetConnBadge(string key)
        {
            _connKey = key;
            Color bg, ink, dot;
            switch (key)
            {
                case "badge_online": bg = NextTheme.OkPale; ink = NextTheme.Ok; dot = NextTheme.Ok; break;
                case "badge_reconnecting": bg = NextTheme.WarnPale; ink = NextTheme.Warn; dot = NextTheme.Warn; break;
                default: bg = NextTheme.IdlePale; ink = NextTheme.InkSoft; dot = NextTheme.Idle; break;
            }
            PaintBadge(_connBadge, NextStrings.Get(key), bg, ink, dot);
            // 斷線橫幅：只在登入中、而且正在重連時掛出來（登入頁的「離線」不算斷線）
            if (_connBanner != null)
            {
                var show = _loggedIn && key == "badge_reconnecting";
                if (_connBanner.gameObject.activeSelf != show)
                {
                    _connBanner.gameObject.SetActive(show);
                    if (_canvas != null) UiBuilder.Settle((RectTransform)_canvas.transform);
                }
            }
            var pulse = _connBadge != null ? _connBadge.Find("TopConnDot") : null;
            if (pulse != null)
            {
                var breathing = pulse.GetComponent<NextPulse>();
                if (key == "badge_online" && breathing == null) pulse.gameObject.AddComponent<NextPulse>();
                if (key != "badge_online" && breathing != null) { Destroy(breathing); pulse.localScale = Vector3.one; }
            }
        }

        /// <summary>身份徽章（主人＝天藍、訪客＝粉）。空 key ⇒ 收起來（登入頁）。</summary>
        private void SetRoleBadge(string key)
        {
            _roleKey = key;
            if (_roleBadge == null) return;
            _roleBadge.gameObject.SetActive(!string.IsNullOrEmpty(key));
            if (string.IsNullOrEmpty(key)) return;
            var master = key == "badge_master";
            PaintBadge(_roleBadge, NextStrings.Get(key), master ? NextTheme.SkyPale : NextTheme.PinkPale,
                       master ? NextTheme.SkyDeep : NextTheme.Pink, null);
        }

        private static void PaintBadge(RectTransform badge, string text, Color bg, Color ink, Color? dot)
        {
            if (badge == null) return;
            var label = badge.GetComponentInChildren<TMP_Text>(true);
            NextUi.SetText(label, text);
            NextUi.SetColor(label, ink);
            var plate = badge.Find(badge.name + "Bg");
            if (plate != null && plate.GetComponent<UnityEngine.UI.Image>() is UnityEngine.UI.Image image) image.color = bg;
            var mark = badge.Find(badge.name + "Dot");
            if (mark != null && dot.HasValue && mark.GetComponent<UnityEngine.UI.RawImage>() is UnityEngine.UI.RawImage raw)
                raw.color = dot.Value;
        }

        /// <summary>
        /// 公共區 Outbox 一筆有了結局：投稿／回覆的提示照實說（送達、等網路、被拒），再讓還開著的頁面重畫。
        /// 「等網路」只在第一次嘗試時說一次，免得離線時每次 Kick 都冒一條。
        /// </summary>
        private void OnSocialSettled(SocialMutation mutation, NextSocialSync.Outcome outcome, string code)
        {
            var writing = mutation.Kind == "post.create" || mutation.Kind == "reply.create";
            if (outcome == NextSocialSync.Outcome.Accepted && mutation.Kind == "post.create")
                Status(NextStrings.Get("feed_posted"));
            else if (outcome == NextSocialSync.Outcome.Deferred && writing && mutation.Attempts == 0)
                Status(NextStrings.Get("feed_queued"));
            else if (outcome == NextSocialSync.Outcome.Rejected && writing)
                Status(NextStrings.F(mutation.Kind == "post.create" ? "feed_publish_fail" : "feed_reply_fail",
                    "reason", code));
            _feed?.OnSocialSettled(mutation, outcome);
            _post?.OnSocialSettled(mutation, outcome);
        }

        /// <summary>一次性的提示（她離席了、進桌失敗、已複製…）：頂欄下面浮出來、幾秒後自己淡掉。空字串不顯示。</summary>
        public void Status(string text)
        {
            NextToast.Show(_bodyHost, text);
        }

        /// <summary>
        /// 軟鍵盤讓位：Unity 在 Android 不做 `adjustResize` ⇒ 鍵盤彈起來時**蓋住打字區**
        /// （2026-10-22 模擬器實測：composer 整條被鍵盤壓住）。
        /// 解法＝把根版面的**下緣內距**撐成鍵盤高度（設計單位）⇒ body 縮短、composer 被頂上來，
        /// topbar 不動。鍵盤收起來就歸零。
        /// </summary>
        private void Update()
        {
            if (_rootGroup == null) return;
            WatchScreen();
            if (_chat != null) _chat.Tick();
            PollNative();
            var pixels = 0f;
            if (TouchScreenKeyboard.visible)
            {
                // `Screen` 座標原點在左下；鍵盤佔 bottom 那一塊 ⇒ 高度直接取 area.height。
                var area = TouchScreenKeyboard.area;
                if (area.height > 0f) pixels = area.height;
            }
            // 設計單位／像素：畫布寬（手機 460、大螢幕＝螢幕的 pt 寬）÷ 螢幕寬
            var designPerPixel = Screen.width > 0 ? CanvasWidth / Screen.width : 0f;
            var target = Mathf.Max(0f, pixels * designPerPixel);
            if (Mathf.Abs(target - _keyboardDesign) < 3f) return;
            _keyboardDesign = target;
            ApplyRootPadding();
            UiBuilder.Settle((RectTransform)_canvas.transform);
        }

        /// <summary>外殼那一圈的邊（border-box 的 1px，見 `UiBuilder.NewCanvas`）。</summary>
        private int _edge;
        private const int TopbarPadLeft = 14;
        private const int TopbarPadRight = 12;

        /// <summary>
        /// 根版面的內距＝外殼的邊＋**系統安全區**（瀏海、挖孔、Home 條），下緣再跟鍵盤取大的。
        /// 以前沒算安全區：iPhone Duo 外屏右上角的鏡頭孔正好壓在設定鈕上（2026-10-30 模擬器實測）。
        /// 底圖不吃版面，照樣鋪滿整塊螢幕。
        ///
        /// ⚠️ **直向時左右的內縮只給頂欄**：Duo 外屏只有右上角一個孔，系統卻報「整條右邊」內縮 84 單位
        /// （實測 safeArea x:0 width:1146／1398）——整頁都讓開會白白少掉快兩成寬，而孔只壓得到頂欄那一列。
        /// 橫向時孔／瀏海在側邊、整列都會被擋 ⇒ 才整頁讓開。
        /// </summary>
        private void ApplyRootPadding()
        {
            if (_rootGroup == null || Screen.width <= 0 || Screen.height <= 0) return;
            var perPixel = CanvasWidth / Screen.width;
            var area = Screen.safeArea;
            var left = Mathf.RoundToInt(area.xMin * perPixel);
            var right = Mathf.RoundToInt((Screen.width - area.xMax) * perPixel);
            var top = Mathf.RoundToInt((Screen.height - area.yMax) * perPixel);
            var bottom = Mathf.RoundToInt(Mathf.Max(area.yMin * perPixel, _keyboardDesign));
            var landscape = Screen.width > Screen.height;
            var sideLeft = landscape ? left : 0;
            var sideRight = landscape ? right : 0;
            if (_topbar != null)
            {
                var bar = _topbar.GetComponent<UnityEngine.UI.HorizontalLayoutGroup>();
                if (bar != null)
                    bar.padding = new RectOffset(TopbarPadLeft + left - sideLeft, TopbarPadRight + right - sideRight, 0, 0);
            }
            var padding = new RectOffset(_edge + sideLeft, _edge + sideRight, _edge + top, _edge + bottom);
            if (padding.ToString() != _rootGroup.padding.ToString())
                Debug.Log($"[next] 安全區 {area}（螢幕 {Screen.width}x{Screen.height}）⇒ 內距 {padding}");
            _rootGroup.padding = padding;
        }

        /// <summary>畫布現在幾個設計單位寬（聊天頁要不要分兩欄看它）。</summary>
        public float CanvasWidth
        {
            get { return _canvas != null ? ((RectTransform)_canvas.transform).sizeDelta.x : UiBuilder.Reference.x; }
        }

        /// <summary>
        /// 螢幕大小變了（Duo 開合、轉向）⇒ 等它停 0.25 秒再重排一次（開合動畫中途會連變好幾次）。
        /// 同時先放開橫向試探、1 秒後再決定收不收（見 `NextLayout.AllowLandscape` 為什麼）。
        /// 一般手機只准直向、螢幕大小不會變 ⇒ 不會走到試探。
        /// </summary>
        private void WatchScreen()
        {
            var now = Time.realtimeSinceStartup;
            var size = new Vector2Int(Screen.width, Screen.height);
            if (size != _screenSize)
            {
                _screenSize = size;
                _relayoutAt = now + 0.25f;
                if (_quietNextChange) _quietNextChange = false;
                else
                {
                    NextLayout.AllowLandscape(true);
                    _orientAt = now + 1f;
                }
            }
            if (_orientAt > 0f && now >= _orientAt)
            {
                _orientAt = 0f;
                NextLayout.ApplyOrientation();
                // 小螢幕卻停在橫向 ⇒ 這一收系統會轉回直向，那一次變化是自己造成的
                if (!Screen.autorotateToLandscapeLeft && Screen.width > Screen.height) _quietNextChange = true;
            }
            if (_relayoutAt <= 0f || now < _relayoutAt) return;
            _relayoutAt = 0f;
            Relayout();
        }

        /// <summary>照新的螢幕重算畫布、重鋪底圖，再重畫這一頁（不能重畫的頁就只重排）。</summary>
        private void Relayout()
        {
            if (_canvas == null) return;
            UiBuilder.DesignWidth = NextLayout.DesignWidth();
            _camera.ResetAspect();
            var root = (RectTransform)_canvas.transform;
            UiBuilder.FitToCamera(root, _camera);
            var backdrop = root.Find("NextBackdrop");
            if (backdrop != null) Destroy(backdrop.gameObject);
            NextUi.Background(root);
            ApplyRootPadding();                    // 安全區跟著螢幕換（外屏有挖孔、內屏沒有）
            Debug.Log($"[next] 螢幕 {Screen.width}x{Screen.height} ⇒ 畫布 {root.sizeDelta} "
                      + $"大螢幕={NextLayout.IsLargeScreen()} 短邊={NextLayout.ShortSidePoints():0}pt "
                      + $"安全區={Screen.safeArea} 內距={_rootGroup.padding}");
            if (_rebuild != null)
            {
                _quietRebuild = true;              // 轉向／開合只是重排同一頁：不掃光
                try { _rebuild(); }
                finally { _quietRebuild = false; }
                return;
            }
            CapBody(_bodyCapped);
            UiBuilder.Settle(root);
        }

        private bool _bodyCapped;

        /// <summary>
        /// 大螢幕上聊天以外的頁面置中、最寬 <see cref="NextLayout.PageMaxWidth"/>（設定、相冊、通話…
        /// 拉成一整排很難看也很難按）；聊天頁自己分欄，不限。手機的畫布本來就只有 460 寬 ⇒ 不受影響。
        /// </summary>
        private void CapBody(bool cap)
        {
            _bodyCapped = cap;
            if (_bodyLayout == null) return;
            var capped = cap && CanvasWidth > NextLayout.PageMaxWidth + 40f;
            // ⚠️ min／preferred 一定要自己給（0）：不給就用內容量出來的寬，橫排不會把它壓到可用寬以下
            // ⇒ 內容寬一點整頁就溢出右邊（模擬器實測：加了安全區之後送出鈕、第三個角色都被切掉）。
            // 以前 body 直接掛在直排的根底下，直排會硬撐滿，所以沒這個問題。
            _bodyLayout.minWidth = 0f;
            _bodyLayout.flexibleWidth = capped ? 0f : 1f;
            _bodyLayout.preferredWidth = capped ? NextLayout.PageMaxWidth : 0f;
        }

        // ── 開機／登入 ───────────────────────────────────────────────────────────

        private void Boot()
        {
            Api.BaseUrl = NextConfig.BaseUrl;
            Api.Token = NextConfig.Token;
            if (!NextConfig.HasToken)
            {
                ShowLogin("");
                return;
            }
            SetConnBadge("badge_connecting");
            Api.Get("/api/me", reply =>
            {
                if (reply.Status == 401)
                {
                    // 契約 §4：401 ⇒ 清掉本機 token，回登入頁
                    NextConfig.ClearToken();
                    NextNotices.Configure(this, false);
                    ShowLogin(NextStrings.Get("login_token_invalid"));
                    return;
                }
                if (!reply.Ok)
                {
                    ShowLogin(NextStrings.F("login_unreachable", "reason", reply.UserMessage()));
                    return;
                }
                Enter(reply.Json);
            });
        }

        private void ShowLogin(string message)
        {
            // 通話中被登出／token 失效：趁 token 還在先掛（不然伺服器要等心跳逾時才收線）
            NextCallView.HangUpActive();
            _loggedIn = false;
            // 這一段登入整個收尾（U-3）：之前出發的請求／位置／補拿回呼全部作廢，
            // 在途的那一輪取消、她的聲音停、執行期 token 與事件游標清掉、一次性明文忘掉。
            Api.Session.Close();
            Api.CancelTurn();
            Api.CancelMediaUploads();
            if (_chat != null) _chat.CancelPendingAggregation();
            Api.StopEvents();
            Api.Cursors.Unbind();
            Api.BindCache("");                     // 本機快取留著（下次同一個人登入還用得到），只是不再指向他
            Api.Token = "";
            NextConfig.LocationSharing = false;  // 只清运行时确认，不向服务端撤销授权。
            LocationSharingBusy = false;
            _locationStatusLoading = false;
            StopLocation();
            StopSpeech();
            NextAccountsView.Forget();
            _chat = null;
            _chatList = null;
            _feed = null;
            _compose = null;
            _post = null;
            _pickForCompose = false;
            LastThread = "";
            ClearBody();
            SetConnBadge("badge_offline");
            SetRoleBadge("");
            SetTopButton(_settingsButton, false);
            SetTopButton(_chatButton, false);
            BuildLogin(message);
        }

        private void BuildLogin(string message)
        {
            ClearBody();
            NextLoginView.Build(_body, message, Connect, Pair, _pendingPairCode);
            _rebuild = () => BuildLogin("");
            UiBuilder.Settle((RectTransform)_canvas.transform);
        }

        /// <summary>二維碼深連結帶進來、還沒兌換的配對碼（登入頁預先填好）。</summary>
        private string _pendingPairCode = "";

        /// <summary>
        /// 用配對碼登錄這一台（owner 2026-11-16：多設備配對）：`POST /api/pair/claim` 換到這台自己的 token，
        /// 再走跟貼 token 一樣的 `Connect`（`/api/me` 成功才落盤）。
        /// </summary>
        private void Pair(string code)
        {
            code = KeepOnly(code ?? "", "0123456789");
            if (code.Length != 6)
            {
                NextLoginView.SetState(NextStrings.Get("login_pair_need"), true);
                return;
            }
            NextLoginView.SetState(NextStrings.Get("connecting"), false);
            Api.BaseUrl = NextConfig.BaseUrl;
            Api.Token = "";
            var body = new JObject().Put("code", code).Put("name", SystemInfo.deviceModel ?? "");
            Api.Post("/api/pair/claim", body, reply =>
            {
                if (!reply.Ok || reply.Json == null)
                {
                    var why = reply.Status == 404 ? NextStrings.Get("login_pair_wrong") : reply.UserMessage();
                    NextLoginView.SetState(why, true);
                    return;
                }
                _pendingPairCode = "";
                Connect(reply.Json.OptString("token"));
            });
        }

        /// <summary>
        /// 深連結 `blueagent://pair?code=123456`（系統相機掃二維碼 → 落地頁 → 跳回這裡）。
        /// 還沒登入 ⇒ 直接兌換；已經登入 ⇒ 不動現在的身分，只提示。
        /// </summary>
        private void OnDeepLink(string url)
        {
            if (string.IsNullOrEmpty(url) || !url.StartsWith("blueagent://pair", StringComparison.OrdinalIgnoreCase)) return;
            var at = url.IndexOf("code=", StringComparison.OrdinalIgnoreCase);
            var code = at < 0 ? "" : KeepOnly(url.Substring(at + 5), "0123456789");
            if (code.Length > 6) code = code.Substring(0, 6);
            if (_loggedIn)
            {
                Status(NextStrings.Get("pair_already_logged_in"));
                return;
            }
            _pendingPairCode = code;
            BuildLogin("");
            Pair(code);
        }

        /// <summary>切換身份：把本機 token 清掉回登入頁（master ↔ guest 換手用，不必清 app 資料）。</summary>
        public void Logout()
        {
            NextConfig.ClearToken();
            NextNotices.Configure(this, false);
            ShowLogin("");
        }

        /// <summary>
        /// 登入：只貼 token。**位址寫死在 `NextConfig.DefaultBaseUrl`**（owner 2026-10-29），
        /// 使用者輸入的位址從此不存在 ⇒ 這裡也沒有位址的正規化與落盤。
        /// </summary>
        private void Connect(string token)
        {
            // ① 只濾掉非 ASCII（日語輸入法會把 `cc` 組成 `っ`；貼上也可能帶全形空白）
            token = KeepOnly(token, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-").Trim();
            if (string.IsNullOrEmpty(token))
            {
                NextLoginView.SetState(NextStrings.Get("login_need_token"), true);
                return;
            }
            NextLoginView.SetState(NextStrings.Get("connecting"), false);
            Api.BaseUrl = NextConfig.BaseUrl;
            Api.Token = token;
            // ⚠️ token 等 `/api/me` 成功才落盤：連不上／錯的 token 不該覆蓋掉上一顆能用的
            Api.Get("/api/me", reply =>
            {
                if (reply.Status == 401)
                {
                    NextLoginView.SetState(NextStrings.Get("login_token_wrong"), true);
                    return;
                }
                if (!reply.Ok)
                {
                    // 連不上（status 0）那句本身就說了「伺服器連不上」，不再套一層前綴
                    var why = reply.UserMessage();
                    NextLoginView.SetState(reply.Status == 0 ? why : NextStrings.F("login_fail", "reason", why), true);
                    return;
                }
                NextConfig.Token = token;
                Enter(reply.Json);
            });
        }

        /// <summary>只留下白名單裡的字元（不合法的一律丟掉，不是報錯——錯字比擋字更難查）。</summary>
        private static string KeepOnly(string text, string allowed)
        {
            if (string.IsNullOrEmpty(text)) return "";
            var buffer = new System.Text.StringBuilder(text.Length);
            foreach (var c in text)
            {
                if (allowed.IndexOf(c) >= 0) buffer.Append(c);
            }
            return buffer.ToString();
        }

        /// <summary>`/api/me` 成功之後：記住這一端、挑 entity、開常駐推播、畫首頁。</summary>
        private void Enter(JObject me)
        {
            Me = me;
            _loggedIn = true;
            NextConfig.Role = me.OptString("role");
            var entities = me.OptJArray("entities");
            Entities = entities;
            // 上次跟哪個角色說話就回到那一個（guest 只有一個，等於沒得選）
            var entity = null as JObject;
            var wanted = NextConfig.Entity;
            for (var i = 0; entities != null && i < entities.Length(); i++)
            {
                var row = entities.OptJObject(i);
                if (row == null) continue;
                if (entity == null) entity = row;
                if (!string.IsNullOrEmpty(wanted) && row.OptString("id") == wanted) { entity = row; break; }
            }
            EntityId = entity != null ? entity.OptString("id") : "mira";
            EntityName = entity != null ? entity.OptString("name") : EntityId;
            if (string.IsNullOrEmpty(EntityName)) EntityName = EntityId;
            NextConfig.Entity = EntityId;

            // 新的一段登入：先開代次，再綁這個人在這台伺服器的事件游標（U-1），最後才開推播與位置
            Api.Session.Open();
            Api.Cursors.Bind(Api.BaseUrl, Principal);
            Api.BindCache(Principal, ChatAccountId); // 快取沿用舊身分槽；會話 id 必須用 canonical account_id。
            // 本機資料庫開不起來就照原文說出來（不要靜靜停在「接續中」）：原文已經在 CacheError。
            if (!string.IsNullOrEmpty(Api.CacheError)) Status(Api.CacheError);
            _chat = new NextChatView();
            _chatList = new NextChatListView();
            _feed = new NextFeedView();
            _compose = new NextComposeView();
            _post = new NextPostView();
            _profile = new NextProfileView();
            Api.SocialSync.Settled -= OnSocialSettled;   // 重新登入不會重複訂閱
            Api.SocialSync.Settled += OnSocialSettled;
            Api.StartEvents(OnEvent, OnEventsState);
            NextNotices.AskPermissionOnce();   // B.App.8：Android 13+ 通知權限（私聊／帖子通知、リマインド共用）
            NextNotices.Configure(this, true);
            RefreshLocationSharing();          // 未确认授权前不会请求系统定位权限。
            if (IsMaster) ShowMaster(); else ShowGuest();
            ResumeSync();
            UiBuilder.Settle((RectTransform)_canvas.transform);
        }

        private void OnEvent(string eventName, JObject payload)
        {
            if (eventName == "location.updated" || eventName == "location.revoked")
            {
                var actor = payload.OptJObject("actor");
                if (eventName == "location.revoked" && actor?.OptString("id") == ChatAccountId)
                {
                    NextConfig.LocationSharing = false;
                    StopLocation();
                    LocationSharingMessage = NextStrings.Get("location_revoked");
                    if (_loggedIn && GuestSettingsOpen && SettingsPage == "service") RenderSettings();
                }
                return;                         // 私有位置事件不进入聊天或公共区未读。
            }
            if ((eventName == "post.created" || eventName == "reply.created" ||
                 eventName == "post.liked" || eventName == "post.deleted") && payload.Has("event_id"))
            {
                Api.Social?.ApplyEvent(payload);       // 與刷新回包同一個 Repository；重放由 event_id 去重
                return;
            }
            if (eventName == "notice.created" && payload.Has("event_id"))
            {
                NextNotices.OnNotice(this, _bodyHost, payload);   // 別人回了／讚了我的帖（B.App.8）
                return;
            }
            if ((eventName == "call.incoming" || eventName == "call.updated") && payload.Has("event_id"))
            {
                // 她打給主人（owner 2026-11-17）：事件只當訊號，還在不在響以伺服器為準（`NextIncomingCall.Check`）
                NextIncomingCall.OnEvent(this, eventName, payload.OptJObject("payload"));
                return;
            }
            if (eventName == "mood.updated" && payload.Has("event_id"))
            {
                Api.Social?.ApplyEvent(payload);         // 公開社交狀態；不進聊天、不觸發 Agent 回合
                NextMoodBadge.OnEvent(payload);
                return;
            }
            if (eventName == "chat.message.created" && payload.Has("event_id"))
            {
                Api.Chat?.ApplyEvent(payload, ChatAccountId);   // store-first：重放由 event_id 去重；群未讀在這裡累加
                _chat?.OnChatMessage(payload);
                _chatList?.RefreshLocal();
                RefreshTopUnread();
                return;
            }
            if (eventName == "chat.message.deleted" && payload.Has("event_id"))
            {
                Api.Chat?.ApplyEvent(payload, ChatAccountId);   // 收回（2026-11-16）：本地那一則換成墓碑
                _chat?.OnChatMessageDeleted(payload);           // 畫面上那幾顆泡泡換成一行「已收回」
                _chatList?.RefreshLocal();
                RefreshTopUnread();
                return;
            }
            // /v1/events 不再承載舊 mood/delivery；未知平台事件不進聊天泡泡。
            RefreshTopUnread();
        }

        private void OnEventsState(bool connected, string text)
        {
            // 401 已經由 `Api.Unauthorized` 收口（同一段登入只一次），這裡不再回登入頁第二次
            if (text == "unauthorized") return;
            if (!_loggedIn) return;
            SetConnBadge(connected ? "badge_online" : "badge_reconnecting");
            // 契約 v2：連回來就把斷線期間漏掉的回合補上（A1）；心情也用快照對帳。
            if (connected && _chat != null) _chat.SyncMissed();
            if (connected) NextMoodBadge.Sync();
            // 連回來就把離線時排著的投稿／聊天送出去（2026-11-17 模擬器實測：以前只有開公共區、再投一篇
            // 才會 Kick ⇒ 網路早就回來了，帖子還一直掛著「送信待ち」）；公共區上次沒拉到的也重拉一次。
            if (connected)
            {
                Api?.SocialSync?.Kick();
                Api?.ChatSync?.Kick();
                _feed?.RetryIfFailed();
            }
        }

        /// <summary>`result`／`/api/state` 回報的 thread（設定 › 診斷看得到）。</summary>
        public void NoteThread(string thread)
        {
            if (string.IsNullOrEmpty(thread)) return;
            LastThread = thread;
        }

        /// <summary>
        /// 回前景／重連後對現況（A3）：`GET /api/state` 取「現在的 thread／待處理／她正在做什麼」。
        /// 不重畫對話（對話有自己的補拿），只更新狀態列與徽章。
        /// </summary>
        public void ResumeSync()
        {
            if (!_loggedIn || Api == null) return;
            Api.Get("/api/state?view=resume&entity=" + UnityEngine.Networking.UnityWebRequest.EscapeURL(EntityId), reply =>
            {
                if (reply.Status == 401) { OnTokenInvalid(); return; }
                if (!reply.Ok) return;
                var threads = reply.Json.OptJArray("threads");
                if (threads != null)
                {
                    var best = 0.0;
                    for (var i = 0; i < threads.Length(); i++)
                    {
                        var row = threads.OptJObject(i);
                        if (row == null) continue;
                        if (row.OptString("entity") != EntityId) continue;
                        var at = row.OptDouble("last_active_at");
                        if (at >= best) { best = at; LastThread = row.OptString("id"); }
                    }
                }
            });
            if (_chat != null) _chat.SyncMissed();
            NextMoodBadge.Sync();
            NextIncomingCall.Check(this);      // 回前景／點系統通知進來：她還在響就顯示來電畫面
            if (Api.EventsState == "off") Api.StartEvents(OnEvent, OnEventsState);
        }

        private void OnApplicationPause(bool paused)
        {
            // 要進背景：把現在的游標交給 Java（B.App.8），它在最後一個 activity onStop 時接手響通知
            if (paused && _loggedIn) NextNotices.Configure(this, true);
            if (!paused) ResumeSync();
        }

        private void OnApplicationFocus(bool focused)
        {
            if (focused) ResumeSync();
        }

        /// <summary>token 失效（任何 401 都在這裡收口；`Api.Unauthorized` 每段登入只叫一次）。</summary>
        public void OnTokenInvalid()
        {
            if (!_loggedIn) return;
            NextConfig.ClearToken();
            NextNotices.Configure(this, false);
            ShowLogin(NextStrings.Get("token_expired"));
        }

        /// <summary>設定頁（兩端都有；master 從分頁、guest 從聊天抬頭的「設定」鈕）。</summary>
        /// <summary>設定裡的子頁（帳號／狀態）：上面一顆「‹ 戻る」回首頁。</summary>
        public void ShowSubPage(string title, Action<RectTransform> build)
        {
            if (!_loggedIn) return;
            ClearBody();
            Stage = null;
            var column = UiBuilder.Column("SubPage", _body, 10f, new RectOffset(12, 12, 10, 12), true, TextAnchor.UpperLeft);
            UiBuilder.Weight(column, 1f);
            NextUi.Header("SubHead", column, title, ShowSettings);
            build(column);
            _rebuild = () => ShowSubPage(title, build);
            NextMotion.Stagger(column, 1);
            UiBuilder.Settle((RectTransform)_canvas.transform);
        }

        public void ShowSettings()
        {
            if (!_loggedIn) return;
            GuestSettingsOpen = true;
            ClearBody();                           // 進設定＝換頁：照常掃光
            BuildSettings();
        }

        /// <summary>只重畫設定頁（切頁籤、改音量／語言）：不掃光，卡片照樣淡入。</summary>
        private void RenderSettings()
        {
            if (!_loggedIn) return;
            GuestSettingsOpen = true;
            ClearBody(sweep: false);
            BuildSettings();
        }

        private void BuildSettings()
        {
            NextSettingsView.Build(_body, this, !IsMaster);
            _rebuild = RenderSettings;
            UiBuilder.Settle((RectTransform)_canvas.transform);
        }

        /// <summary>聊天選圖：回來仍走原來的延時聚合，不經過公共區。</summary>
        public void PickImage()
        {
            _pickForCompose = false;
            NativeShell.Init();
            NativeShell.PickImage();
            Status(NextStrings.Get("photo_picking"));
        }

        /// <summary>投稿選圖：留在投稿頁，先上傳成 media id，由使用者決定何時正式投稿。</summary>
        public void PickComposeImage()
        {
            _pickForCompose = true;
            NativeShell.Init();
            NativeShell.PickImage();
            Status(NextStrings.Get("photo_picking"));
        }

        /// <summary>原生殼的事件（選圖完成／取消…）：一幀拉一筆，`type` 分派。</summary>
        private void PollNative()
        {
            var raw = NativeShell.Poll();
            if (string.IsNullOrEmpty(raw)) return;
            var e = new JObject(raw);
            switch (e.OptString("type"))
            {
                case "imageSaved":
                    var forCompose = _pickForCompose;
                    _pickForCompose = false;
                    var path = e.OptString("path");
                    if (string.IsNullOrEmpty(path) || !System.IO.File.Exists(path))
                    {
                        Status(NextStrings.Get("photo_read_fail"));
                        return;
                    }
                    Status(NextStrings.Get("photo_sending"));
                    var bytes = System.IO.File.ReadAllBytes(path);
                    // iOS 的挑選器把圖寫成暫存 jpg（`ba-pick-*.jpg`）：讀完就清掉
                    if (System.IO.Path.GetFileName(path).StartsWith("ba-pick-"))
                    {
                        try { System.IO.File.Delete(path); }
                        catch (Exception error) { Debug.LogWarning("[next] 暫存圖刪不掉：" + error.Message); }
                    }
                    if (forCompose)
                    {
                        _compose?.AddPickedImage(bytes);
                    }
                    else
                    {
                        // 聊天選圖回原聊天頁；投稿選圖絕不走這條導航。
                        CloseAlbum();
                        _chat?.SendPickedImage(bytes, "");
                    }
                    break;
                case "pickCancelled":
                    _pickForCompose = false;
                    Status(NextStrings.Get("photo_pick_cancelled"));
                    break;
            }
        }

        private void ShowMasterOrGuest()
        {
            // 通話、棋局、相簿等是從聊天進來的，收掉時仍回**那一個聊天**，不被新的預設首頁改道。
            // ⚠️ 以前開了會話列表就回列表 ⇒ 撥號失敗的理由（`Chat.Notice`）沒地方顯示，
            //    主人只看到「點通話就退回列表」（2026-11-08 實機：其實是 503）。
            if (!string.IsNullOrEmpty(EntityId) || !NextConfig.ChatList) ShowChat(); else ShowChatList();
        }

        /// <summary>相册（P4）：從聊天頁的「＋」進來。</summary>
        public void ShowAlbum()
        {
            if (!_loggedIn) return;
            ClearBody();
            Stage = null;
            NextAlbumView.Build(_body, this);
            _rebuild = ShowAlbum;
            UiBuilder.Settle((RectTransform)_canvas.transform);
        }

        public void CloseAlbum()
        {
            if (IsMaster) ShowMaster(); else ShowGuest();
        }

        /// <summary>單人通話（只給 master，owner 2026-10-29）：從聊天頁「+」進來，掛斷就回聊天頁。</summary>
        public void ShowCall()
        {
            if (!_loggedIn || !IsMaster) return;
            ClearBody();
            Stage = null;
            NextCallView.Build(_body, this);
            UiBuilder.Settle((RectTransform)_canvas.transform);
        }

        /// <summary>
        /// 她打來、主人按了「応答」（伺服器已轉 live）：切到那個角色、直接進通話畫面（不再撥號）。
        /// </summary>
        public void AnswerCall(string callId, string entity)
        {
            if (!_loggedIn || !IsMaster) return;
            if (!string.IsNullOrEmpty(entity) && entity != EntityId)
            {
                EntityId = entity;
                EntityName = EntityNameOf(entity);
                if (IsAgent(entity)) NextConfig.Entity = entity;
                LastThread = "";
                StopSpeech();
            }
            ClearBody();
            Stage = null;
            NextCallView.BuildAnswered(_body, this, callId);
            UiBuilder.Settle((RectTransform)_canvas.transform);
        }

        /// <summary>全屏浮層（來電畫面）掛在畫布最上面；一次性提示掛在 body 外層。</summary>
        public Transform OverlayRoot => _canvas != null ? _canvas.transform : null;
        public RectTransform ToastHost => _bodyHost;

        public void CloseCall()
        {
            if (!_loggedIn) return;
            ShowMasterOrGuest();
        }

        /// <summary>看全圖：蓋一層黑底＋整張圖，點任何地方關掉。</summary>
        public void ShowMediaFull(string mediaId)
        {
            // 疊在整張畫布最上面、不參與版面（2026-10-29 模擬器實測：以前是 `_body` 底下的一欄，
            // 會被當成版面裡的一項排在相冊下面，只露出下半截）
            var overlay = UiBuilder.Overlay("MediaFull", _canvas.transform);
            var bg = overlay.gameObject.AddComponent<UnityEngine.UI.Image>();
            bg.color = new Color(0f, 0f, 0f, 0.92f);
            bg.raycastTarget = true;
            var back = overlay.gameObject.AddComponent<UnityEngine.UI.Button>();
            back.targetGraphic = bg;
            back.onClick.AddListener(() => { Destroy(overlay.gameObject); });

            var host = new GameObject("MediaFullImage", typeof(RectTransform), typeof(UnityEngine.UI.Image));
            var rect = (RectTransform)host.transform;
            rect.SetParent(overlay, false);
            rect.anchorMin = new Vector2(0.04f, 0.06f);
            rect.anchorMax = new Vector2(0.96f, 0.94f);
            rect.offsetMin = Vector2.zero;
            rect.offsetMax = Vector2.zero;
            var image = host.GetComponent<UnityEngine.UI.Image>();
            image.preserveAspect = true;
            image.raycastTarget = false;
            Api.GetMediaTexture(mediaId, texture =>
            {
                // 回來之前 overlay 已經被點掉 ⇒ image 已銷毀：貼圖就地釋放，不寫已銷毀的物件（U-7）
                NextUi.SetOwnedSprite(image, texture);
            });
            UiBuilder.Settle((RectTransform)_canvas.transform);
        }

        /// <summary>選一個場景包（空字串＝なし）；改完立刻重畫聊天頁。</summary>
        public void SetBackground(string id)
        {
            NextConfig.SetBackgroundFor(EntityId, id);
            if (IsMaster) ShowMaster(); else ShowGuest();
            ShowSettingsPage("bg");
        }

        /// <summary>隨機換一個場景包（P3：不用 settings 那頁的「下一張」語意，直接抽一個不同的）。</summary>
        public void RandomBackground()
        {
            var ids = new[] { "school", "office", "street", "aster" };
            var current = NextConfig.BackgroundFor(EntityId);
            var pool = System.Array.FindAll(ids, x => x != current);
            if (pool.Length == 0) return;
            var pick = pool[UnityEngine.Random.Range(0, pool.Length)];
            SetBackground(pick);
        }

        /// <summary>設定內部頁籤（音声／気分／背景／サービス／診断）。</summary>
        public void ShowSettingsPage(string page)
        {
            SettingsPage = string.IsNullOrEmpty(page) ? "audio" : page;
            if (SettingsPage == "service" && HealthCounts == null)
            {
                Api.Get("/api/health", reply => { if (reply.Ok) HealthCounts = reply.Json; });
            }
            if (SettingsPage == "mood") RefreshMoodSettings();
            if (SettingsPage == "service") RefreshLocationSharing();
            RenderSettings();
        }

        private void RefreshMoodSettings()
        {
            if (_moodSettingsLoading || Api.Social == null || string.IsNullOrEmpty(ChatAccountId)) return;
            _moodSettingsLoading = true;
            var version = NextMoodBadge.Version;
            var repository = Api.Social;
            var accountId = ChatAccountId;
            Api.Get("/v1/me/mood", reply =>
            {
                _moodSettingsLoading = false;
                if (repository == null || !ReferenceEquals(repository, Api.Social)) return;
                if (reply.Ok)
                {
                    var mood = reply.Json.OptJObject("mood");
                    if (mood != null) repository.MergeMood(mood); // 時間戳會擋住較舊回包。
                    else if (version == NextMoodBadge.Version) repository.DeleteMood(accountId);
                }
                if (_loggedIn && SettingsPage == "mood") RenderSettings();
            });
        }

        /// <summary>主人與客人設定自己的公開心情；六個預設不需要文字輸入。</summary>
        public void SetMyMood(string key)
        {
            if (MoodSettingBusy || Api.Social == null || string.IsNullOrEmpty(ChatAccountId)) return;
            var current = Api.Social.Mood(ChatAccountId);
            var body = new JObject().Put("key", key ?? "")
                .Put("note", current == null ? "" : current.OptString("note"));
            MoodSettingBusy = true;
            MoodSettingError = false;
            MoodSettingMessage = NextStrings.Get("mood_saving");
            RenderSettings();
            Api.Put("/v1/me/mood", body, reply =>
            {
                MoodSettingBusy = false;
                if (reply.Ok)
                {
                    var mood = reply.Json.OptJObject("mood");
                    if (mood != null) Api.Social?.MergeMood(mood);
                    MoodSettingError = false;
                    MoodSettingMessage = NextStrings.Get("mood_saved");
                }
                else
                {
                    MoodSettingError = true;
                    MoodSettingMessage = NextStrings.F("mood_save_fail", "reason", reply.UserMessage());
                }
                if (_loggedIn && SettingsPage == "mood") ShowSettingsPage("mood");
            });
        }

        /// <summary>音量 ±0.2（套在 SpeechPlayer；範圍 0..1）。</summary>
        public void NudgeVolume(float delta)
        {
            NextConfig.Volume = Mathf.Clamp01(NextConfig.Volume + delta);
            if (Voice != null) Voice.Volume = NextConfig.Volume;
            ShowSettingsPage("audio");
        }

        /// <summary>界面語言循環：ja → zh-Hans → zh-Hant。</summary>
        public void CycleLanguage()
        {
            ApplyLanguage(NextStrings.Next(NextConfig.UiLanguage));
            ShowSettingsPage("audio");
        }

        /// <summary>
        /// 換界面語言：頁面每次都重建，自然換字；**常駐的頂欄**（設定鈕、連線徽章、狀態列）只建一次，
        /// 要在這裡照原樣重套一次，不然換成中文後頂欄還留著日語（2026-10-28 無頭倒字抓到）。
        /// </summary>
        public void ApplyLanguage(string locale)
        {
            NextConfig.UiLanguage = locale;
            NextFonts.ApplyUi();
            NextFonts.Retint(_topbar);
            if (_connBanner != null) NextFonts.Retint(_connBanner);
            if (_connBannerText != null) NextUi.SetText(_connBannerText, NextStrings.Get("conn_banner"));
            SetConnBadge(_connKey);
            SetRoleBadge(_roleKey);
        }

        public void CloseSettings()
        {
            GuestSettingsOpen = false;
            if (IsMaster) ShowMasterTab(MasterTab);
            else ShowGuest();
        }

        // ── 兩端 ────────────────────────────────────────────────────────────────

        private void ShowMaster()
        {
            SetRoleBadge("badge_master");
            ShowHome();
        }

        private void ShowGuest()
        {
            SetRoleBadge("badge_guest");
            ShowHome();
        }

        /// <summary>M3 預設首頁＝公共區；設定關掉時完整退回 M1。</summary>
        public void ShowHome()
        {
            if (NextConfig.HomeIsFeed) ShowFeed();
            else if (NextConfig.ChatList) ShowChatList();
            else ShowChat();
        }

        public void ShowFeed()
        {
            if (!_loggedIn || _feed == null) return;
            ClearBody();
            CapBody(true);                         // 公共区不掛舞台：大螢幕也是置中限寬的單欄流
            Stage = null;
            _feed.Build(_body, this);
            _rebuild = ShowFeed;
            _here = ShowFeed;
            SetTopButton(_settingsButton, true);
            SetTopButton(_chatButton, true);
            RefreshTopUnread();
            UiBuilder.Settle((RectTransform)_canvas.transform);
        }

        public void ShowCompose()
        {
            if (!_loggedIn || _compose == null) return;
            ClearBody();
            Stage = null;
            _compose.Build(_body, this);
            // 打字與圖片是未送出草稿，旋轉時只重排，不重建清空。
            UiBuilder.Settle((RectTransform)_canvas.transform);
        }

        /// <param name="keepBack">由「返回」重開這一頁時為真：沿用它原本的返回目標，
        /// 不然 主頁→返回→詳情 會把主頁記成詳情的返回，兩頁互相來回（2026-11-08 模擬器實測）。</param>
        public void ShowPost(string id, bool keepBack = false)
        {
            if (!_loggedIn || _post == null || string.IsNullOrEmpty(id)) return;
            ClearBody();
            Stage = null;
            if (!keepBack || PostBack == null) PostBack = _here ?? ShowFeed;
            _post.Build(_body, this, id);
            _here = () => ShowPost(id, true);
            // 回覆草稿同樣不因螢幕尺寸變化被清掉。
            UiBuilder.Settle((RectTransform)_canvas.transform);
        }

        /// <summary>個人主頁（owner 2026-11-08）：點帖子卡／詳情裡的頭像進來；返回回到剛才那一頁。</summary>
        public void ShowProfile(JObject who)
        {
            if (!_loggedIn || _profile == null || who == null || string.IsNullOrEmpty(who.OptString("author_id")))
                return;
            if (_profile.Alive && _profile.AccountId == who.OptString("author_id")) return;   // 主頁裡點同一人
            var back = _here ?? ShowFeed;
            UnityEngine.Events.UnityAction open = null;
            open = () =>
            {
                ClearBody();
                CapBody(true);
                Stage = null;
                _profile.Build(_body, this, who, back);
                _rebuild = () => open();
                _here = open;                  // 主頁是子頁：設定鈕照設計稿只在首頁／會話列表／聊天頁
                UiBuilder.Settle((RectTransform)_canvas.transform);
            };
            open();
        }

        /// <summary>M1 會話列表；M3 從公共區的聊天鈕進來。</summary>
        public void ShowChatList()
        {
            if (!_loggedIn || _chatList == null) return;
            ClearBody();
            CapBody(true);
            Stage = null;
            _chatList.Build(_body, this);
            _rebuild = ShowChatList;
            SetTopButton(_settingsButton, true);
            UiBuilder.Settle((RectTransform)_canvas.transform);
        }

        /// <summary>具體 1:1 聊天頁：仍只使用原有的 `NextChatView`。</summary>
        private void ShowChat()
        {
            ClearBody();
            CapBody(false);                        // 聊天頁自己分欄（大螢幕左立繪右對話），不限寬
            _chat.Build(_body, this);
            _rebuild = ShowChat;
            SetTopButton(_settingsButton, _loggedIn);
            UiBuilder.Settle((RectTransform)_canvas.transform);
        }

        /// <summary>master 分頁切換（由 `NextMasterView` 的導覽列叫）。</summary>
        public void ShowMasterTab(string tab)
        {
            MasterTab = tab;
            ShowHome();
        }

        public NextChatView Chat { get { return _chat; } }

        /// <summary>
        /// 角色的**顯示名**（給人看的，跟界面語言走）：表裡有 `ent_{id}` 就用它（ミラ／米拉…），
        /// 沒有才退回伺服器的名字、再退回 id。
        ///
        /// 為什麼分兩個名字（owner 2026-10-29「角色名分為後台名和顯示名，在前台做 i18n」）：
        /// 伺服器的 `name`（<see cref="EntityName"/>）是後台名——人格檔、提示詞、記憶都認它，不能為了
        /// 介面換語言去改；畫面上要的是這個人用自己的語言叫得出來的名字，那是前台的事。
        /// </summary>
        // ── 人與人之間的私聊（P-C2，2026-11-08）────────────────────────────────
        /// <summary>見過的「人」（主人／客人）：id → (kind, display)。會話列表的 members 與個人主頁會補進來。</summary>
        private readonly Dictionary<string, KeyValuePair<string, string>> _people =
            new Dictionary<string, KeyValuePair<string, string>>();

        public void RememberPerson(string id, string kind, string display)
        {
            if (string.IsNullOrEmpty(id) || kind == "entity" || IsAgent(id)) return;
            _people[id] = new KeyValuePair<string, string>(kind ?? "", string.IsNullOrEmpty(display) ? id : display);
        }

        public string PersonKind(string id)
        {
            return id != null && _people.TryGetValue(id, out var person) ? person.Key : "";
        }

        /// <summary>這個 id 是角色嗎（`/api/me` 的 entities 裡有它）；群鍵與人都不是。</summary>
        public bool IsAgent(string id)
        {
            if (string.IsNullOrEmpty(id) || ChatRepository.IsGroupKey(id)) return false;
            for (var i = 0; Entities != null && i < Entities.Length(); i++)
                if (Entities.OptJObject(i)?.OptString("id") == id) return true;
            return false;
        }

        /// <summary>目前的聊天頁是跟「人」的私聊（主人↔客人）：沒有舞台、沒有語音、不經 Agent。</summary>
        public bool IsPersonChat => !string.IsNullOrEmpty(EntityId) && !ChatRepository.IsGroupKey(EntityId)
                                    && !IsAgent(EntityId);

        /// <summary>從個人主頁開主人↔客人的私聊（會話由 App Server 冪等建立，Agent 看不到）。</summary>
        public void OpenDirect(string id, string kind, string display)
        {
            RememberPerson(id, kind, display);
            SwitchEntity(id, DisplayNameOf(id));
        }

        public string DisplayNameOf(string id)
        {
            if (string.IsNullOrEmpty(id)) return "";
            if (_people.TryGetValue(id, out var person))
                return person.Key == "owner" ? NextStrings.Get("owner_name") : person.Value;
            // 主人自己（舊 principal `owner` 或平台帳號 id）在畫面上一律叫「オーナー／老师」（owner 2026-11-08）
            if (id == "owner" || (IsMaster && (id == Principal || id == ChatAccountId)))
                return NextStrings.Get("owner_name");
            if (NextStrings.Has("ent_" + id)) return NextStrings.Get("ent_" + id);
            return EntityNameOf(id);
        }

        /// <summary>現在這個角色的顯示名（見 <see cref="DisplayNameOf"/>）。</summary>
        public string EntityDisplayName => DisplayNameOf(EntityId);

        /// <summary>聊天页标题：固定群使用会话名，1:1 仍走角色显示名与界面语言。</summary>
        public string ConversationDisplayName =>
            ChatRepository.IsGroupKey(EntityId) ? EntityName : EntityDisplayName;

        /// <summary>某個 entity id 的**後台名**（`/api/me` 那張表；查不到就回 id）。畫面上要用 <see cref="DisplayNameOf"/>。</summary>
        public string EntityNameOf(string id)
        {
            if (string.IsNullOrEmpty(id)) return "";
            if (Entities != null)
            {
                for (var i = 0; i < Entities.Length(); i++)
                {
                    var row = Entities.OptJObject(i);
                    if (row == null || row.OptString("id") != id) continue;
                    var name = row.OptString("name");
                    return string.IsNullOrEmpty(name) ? id : name;
                }
            }
            return id;
        }

        /// <summary>
        /// 換角色（B3，只有 master 會用到）：換 entity ⇒ 重畫聊天頁（訊息與游標都跟著換），
        /// 立繪由新的 `StageAvatar` 依該角色的角色卡重載。
        /// </summary>
        public void SwitchEntity(string id, string name)
        {
            if (string.IsNullOrEmpty(id)) return;
            if (id != EntityId)
            {
                EntityId = id;
                EntityName = string.IsNullOrEmpty(name) ? id : name;
                if (IsAgent(id)) NextConfig.Entity = id;   // 人的私聊不記成「上次的角色」
                LastThread = "";
                Stage = null;
                StopSpeech();                   // 上一個角色還沒播完的那幾段不在新角色的頁面上繼續講
            }
            ShowChat();                         // 列表點同一角色也要進頁；角色 pill 切換不能跳回列表
            ResumeSync();
        }

        /// <summary>打开固定 Agent 群：复用同一聊天页，但不覆盖最近使用的 1:1 角色、不同步旧 Agent thread。</summary>
        public void SwitchGroup(string conversationKey, string name)
        {
            if (!ChatRepository.IsGroupKey(conversationKey)) return;
            if (conversationKey != EntityId)
            {
                EntityId = conversationKey;
                EntityName = string.IsNullOrEmpty(name) ? conversationKey : name;
                LastThread = "";
                Stage = null;
                StopSpeech();
            }
            ShowChat();
        }

        /// <summary>把立繪切到 thinking／speaking／normal（聊天頁在說話時叫）。</summary>
        public void SetStage(string state)
        {
            if (NextStage3D.Current != null) NextStage3D.Current.SetState(state);
            if (Stage == null) return;
            Debug.Log("[next] 立繪 state=" + state);
            Stage.Apply(NextStageView.View(EntityId, state));
        }

        /// <summary>解開過的動作片段（id → 片段）；同一段動作庫裡的動作會一再出現，不必每次重解。</summary>
        private readonly Dictionary<string, MotionClip> _motions = new Dictionary<string, MotionClip>();
        private const int MaxMotions = 24;

        /// <summary>
        /// 這一輪的 `motion` 幀（後端 Kimodo；`docs/PLAN-motion-kimodo-20261031.md` §5.4）：
        /// 只有 3D 模式、而且是現在舞台上的角色才播；片段用 `url` 抓（ETag 快取，抓過就不再下載）。
        /// </summary>
        public void PlayMotion(JObject payload)
        {
            if (payload == null || NextStage3D.Current == null) return;
            var entity = payload.OptString("entity");
            if (!string.IsNullOrEmpty(entity) && entity != EntityId) return;
            var id = payload.OptString("id");
            var url = payload.OptString("url");
            if (string.IsNullOrEmpty(url)) return;
            var stage = NextStage3D.Current;
            if (!string.IsNullOrEmpty(id) && _motions.TryGetValue(id, out var known))
            {
                stage.PlayMotion(known);
                return;
            }
            var cached = Api.CachedBytes(url, payload.OptString("hash"));
            if (cached != null)
            {
                PlayMotionBytes(stage, cached);
                return;
            }
            Api.GetBytes(url, bytes =>
            {
                // 抓回來時已經換了角色／切回 2D ⇒ 不播
                if (bytes != null && stage != null && stage == NextStage3D.Current) PlayMotionBytes(stage, bytes);
            });
        }

        private void PlayMotionBytes(NextStage3D stage, byte[] bytes)
        {
            MotionClip clip;
            try
            {
                clip = MotionClip.Parse(System.Text.Encoding.UTF8.GetString(bytes));
            }
            catch (Exception error)
            {
                Debug.LogWarning("[next] 3D 動作片段解不開：" + error.Message);
                return;
            }
            if (!string.IsNullOrEmpty(clip.Id))
            {
                if (_motions.Count >= MaxMotions) _motions.Clear();
                _motions[clip.Id] = clip;
            }
            stage.PlayMotion(clip);
        }

        public void PlaySpeech(string base64)
        {
            if (string.IsNullOrEmpty(base64) || Voice == null) return;
            // ⚠️ 語音回覆會來**兩段**（短應答＋完整答案）。直接播第二段會把第一段切掉，
            // 所以排隊依序播：一段播完再播下一段。
            _speechQueue.Enqueue(base64);
            if (_speechPump == null) _speechPump = StartCoroutine(PumpSpeech());
        }

        /// <summary>她現在有沒有在出聲（排隊中的也算）——通話的打斷判斷用。</summary>
        public bool IsSpeaking { get { return _speechQueue.Count > 0 || (Voice != null && Voice.IsPlaying); } }

        /// <summary>停掉排隊中與正在播的語音（登出／換角色／通話中被主人打斷）。</summary>
        public void StopSpeech()
        {
            _speechQueue.Clear();
            if (_speechPump != null) StopCoroutine(_speechPump);
            _speechPump = null;
            if (Voice != null) Voice.Stop();
        }

        private IEnumerator PumpSpeech()
        {
            while (_speechQueue.Count > 0)
            {
                var clip = _speechQueue.Dequeue();
                try { Voice.PlayBase64(clip, "next-speech"); }
                catch (Exception error) { Debug.LogWarning("[next] 播放失敗：" + error.Message); }
                while (Voice != null && Voice.IsPlaying) yield return null;
                yield return null;
            }
            _speechPump = null;
        }

        /// <summary>
        /// 換頁的唯一入口：拆掉舊頁、新頁淡入，並**統一掃一道斜光**（owner 2026-11-07：轉場要覆蓋所有頁）。
        /// 不掃的只有兩種「原地重畫」：轉向／開合的 `Relayout`（`_quietRebuild`），
        /// 與設定頁內切頁籤／改音量語言（`RenderSettings` 傳 `sweep: false`）。
        /// </summary>
        private void ClearBody(bool sweep = true)
        {
            if (sweep && !_quietRebuild) Sweep();
            // 頁面離開後，同角色的 delivery 也要留在未讀窗口，不能誤當成正在看。
            if (_chat != null) _chat.SetChatVisible(false);
            // 投稿上傳可繼續完成，但舊頁的輸入框與縮圖回呼立即失效。
            if (_compose != null) _compose.Deactivate();
            if (_body == null) return;
            for (var i = _body.childCount - 1; i >= 0; i--) Destroy(_body.GetChild(i).gameObject);
            _rebuild = null;
            CapBody(true);
            _enterPending = true;
            // 右上角的設定鈕只屬於首頁（聊天頁）：設定頁、加號選單（相簿）、棋局、桌面都不該再有一顆
            // （owner 2026-10-29）⇒ 換頁一律先收起來，聊天頁建好才放出來（見 `ShowChat`）。
            SetTopButton(_settingsButton, false);
            SetTopButton(_chatButton, false);
        }

        /// <summary>
        /// 頂欄按鈕的顯示：登入後**一直佔位**，只用透明度與點擊開關。以前直接 SetActive(false)，
        /// 投稿頁／詳情頁收起設定鈕時，左邊的身分與連線徽章會整個往右跳（2026-11-07 review）。
        /// </summary>
        private void SetTopButton(UnityEngine.UI.Button button, bool visible)
        {
            if (button == null) return;
            if (!_loggedIn) { button.gameObject.SetActive(false); return; }
            button.gameObject.SetActive(true);
            var group = button.GetComponent<CanvasGroup>() ?? button.gameObject.AddComponent<CanvasGroup>();
            group.alpha = visible ? 1f : 0f;
            group.interactable = visible;
            group.blocksRaycasts = visible;
        }

        private void BuildChatBadge()
        {
            var host = (RectTransform)_chatButton.transform;
            _chatBadge = UiBuilder.NewRect("TopChatBadge", host);
            var layout = _chatBadge.gameObject.AddComponent<LayoutElement>();
            layout.ignoreLayout = true;
            _chatBadge.anchorMin = _chatBadge.anchorMax = new Vector2(1f, 1f);
            _chatBadge.pivot = new Vector2(0.5f, 0.5f);
            _chatBadge.anchoredPosition = new Vector2(-4f, -4f);
            _chatBadge.sizeDelta = new Vector2(18f, 18f);
            var disc = _chatBadge.gameObject.AddComponent<UnityEngine.UI.Image>();
            UiBuilder.Round(disc, 9f);
            disc.color = NextTheme.Pink;
            disc.raycastTarget = false;
            _chatBadgeText = UiBuilder.Text("TopChatBadgeText", _chatBadge, "", 10f, NextTheme.OnColor,
                                            TextAlignmentOptions.Center, noWrap: true, bold: true);
            var textRect = (RectTransform)_chatBadgeText.transform;
            var textLayout = textRect.GetComponent<LayoutElement>() ?? textRect.gameObject.AddComponent<LayoutElement>();
            textLayout.ignoreLayout = true;
            UiBuilder.Stretch(textRect);
            _chatBadge.gameObject.SetActive(false);
        }

        /// <summary>頂欄聊天鈕上的未讀數（1:1 的端側未讀合計；群組首期不計）。</summary>
        public void RefreshTopUnread()
        {
            if (_chatBadge == null) return;
            // 1:1 走 ConversationStore，群走本地庫裡的服務端摘要（2026-11-08 接上）
            var unread = (_chat != null ? _chat.UnreadTotal : 0) + (Api.Chat != null ? Api.Chat.GroupUnread() : 0);
            _chatBadge.gameObject.SetActive(unread > 0);
            if (unread > 0) NextUi.SetText(_chatBadgeText, unread > 99 ? "99+" : unread.ToString());
        }

        /// <summary>換頁後第一個 LateUpdate：新頁面淡入＋微放大（一處掛，所有頁都有，不必各頁自己叫）。</summary>
        private bool _enterPending;

        /// <summary>`Relayout` 重畫同一頁期間為真：不掃光（不是換頁）。</summary>
        private bool _quietRebuild;

        private void LateUpdate()
        {
            if (!_enterPending || _body == null || _body.childCount == 0) return;
            _enterPending = false;
            foreach (Transform child in _body)
            {
                if (child is RectTransform page) NextMotion.Enter(page);
            }
        }

#if BA_SELFTEST
        /// <summary>M0 定向驗收只讀正式聊天控制器；正式包不含這個入口。</summary>
        public NextChatView SelfTestChat { get { return _chat; } }

        /// <summary>M1 驗收：開正式列表，注入同形狀回包，仍由 `NextChatListView.Render` 建畫面。</summary>
        public void SelfTestShowSessions(JObject reply)
        {
            ShowChatList();
            _chatList?.SelfTestRender(reply);
        }

#endif

        /// <summary>斜向掃光；只由 `ClearBody` 呼叫，各頁不要自己叫（避免重複或漏掉）。</summary>
        private void Sweep()
        {
            if (_canvas != null && _loggedIn) NextMotion.Wipe((RectTransform)_canvas.transform);
        }

        /// <summary>
        /// 頁面拿到的回包是 401 ⇒ 回 true（已處理）。登入中的 401 其實在 `NextApi` 就收口了、
        /// 回包根本不會送到頁面；這裡只是保險，走同一個 `OnTokenInvalid`。
        /// </summary>
        public bool HandleAuth(NextApi.Reply reply)
        {
            if (reply == null || reply.Status != 401) return false;
            OnTokenInvalid();
            return true;
        }

        /// <summary>403 只提示、不回登入頁（契約 §4）。</summary>
        public bool HandleForbidden(NextApi.Reply reply, string where)
        {
            if (reply == null || reply.Status != 403) return false;
            Status(NextStrings.F("forbidden_at", "where", where));
            return true;
        }
    }
}
