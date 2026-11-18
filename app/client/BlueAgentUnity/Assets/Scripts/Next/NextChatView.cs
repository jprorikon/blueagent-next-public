using System;
using System.Collections.Generic;
using BlueAgent.Logic;
using BlueAgent.Screens;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>
    /// 對話（master／guest 共用同一份畫面）。
    ///
    /// 契約來源：`APP-API-SSE.md` §2 的事件表——`accepted`（記 turn id）／`delta`（逐字追加）／
    /// `done`（用全文校正）／`speech`（播 wav）／`interrupted`（不是錯誤）／`error`；
    /// **契約 v2**：`accepted` 之後斷線 ⇒ 重連用 `GET /api/turns?since=last_good` 補回來（A1）。
    ///
    /// 這一輪只做文字＋聲音（不做嘴型／立繪）；聲音走 `SpeechPlayer.PlayBase64`／`PlayWav`。
    /// </summary>
    public sealed class NextChatView
    {
        private sealed class Msg
        {
            public bool Mine;
            public string Text = "";
            /// <summary>群聊气泡使用真实作者；1:1 留空时仍回退当前 entity。</summary>
            public string AuthorId = "";
            public string Note = "";
            /// <summary>表情包那一顆：新圖走 `/v1/stickers/…`，歷史 `/api/stickers/…` 仍兼容；空＝一般文字泡泡。</summary>
            public string Sticker = "";
            /// <summary>系統標記（例：通話結束、日期分隔）：置中小字、沒有泡泡底，也不是她說的話。</summary>
            public bool Marker;
            /// <summary>日期分隔那一行（`Marker` 也是 true）：載入更早的歷史時要能認出來合併。</summary>
            public string DayKey = "";
            /// <summary>這一則的時間（epoch 秒；0＝不知道，不畫時間）。</summary>
            public double At;
            public string TurnId = "";
            public TMP_Text Label;
            public RectTransform Row;
        }

        private readonly List<Msg> _messages = new List<Msg>();
        /// <summary>已經在畫面上的 turn（補拿時用來去重；文字與音檔各一次）。</summary>
        private readonly HashSet<string> _shownTurns = new HashSet<string>();
        /// <summary>暫時畫上去的「說到一半」分句（正式那一列到了就換掉，避免同一個 turn 出現兩份）。</summary>
        private readonly System.Collections.Generic.Dictionary<string, System.Collections.Generic.List<Msg>>
            _provisional = new System.Collections.Generic.Dictionary<string, System.Collections.Generic.List<Msg>>();

        private NextShell _shell;
        private RectTransform _list;
        private ScrollRect _scroll;
        private TMP_InputField _input;
        private Component _hint;
        // ── 語音模式（按住說話）──
        private Button _micToggle;
        private Button _keyboardToggle;
        private RectTransform _hold;
        private NextGradient _holdPlate;
        private Component _holdLabel;
        private RawImage _holdIcon;
        private bool _voiceMode;
        /// <summary>按住錄好、還沒送出的那一段（按「送信」才送；再按住會重錄；切回打字就丟掉）。</summary>
        private byte[] _pendingWav;
        private float _pendingSeconds;
        /// <summary>目前這張 turn ticket 是不是語音請求；圖片／文字的 accepted 不能清掉待送錄音。</summary>
        private bool _sendingVoice;
        /// <summary>「+」的底部選單（QQ 那種從打字列下面彈起來，不另開一頁）。</summary>
        private readonly NextStickers _stickers = new NextStickers();
        private RectTransform _sheet;
        private LayoutElement _sheetHeight;
        private CanvasGroup _sheetGroup;
        private RectTransform _plusIcon;
        private bool _sheetOpen;
        /// <summary>選單打開時的高度：按排數算（一排 112，兩排再加一排＋排距；多頁再加圓點列）。</summary>
        private float _sheetOpenHeight = SheetRowHeight + SheetPadHeight;
        /// <summary>第一排圖示（方塊 54＋字＋間距）的高度；之後每多一排只加 <see cref="SheetExtraRowHeight"/>。</summary>
        private const float SheetRowHeight = 90f;
        /// <summary>第二排起每排的高度（含排距）。90 會在第二排下面空出約 30 pt（模擬器實測）。</summary>
        private const float SheetExtraRowHeight = 82f;
        /// <summary>選單上下內距（14＋8）。</summary>
        private const float SheetPadHeight = 22f;
        /// <summary>一頁幾排、一排幾個（owner 2026-11-03「做成兩排、可以橫向滑動換頁」）。</summary>
        private const int SheetRows = 2;
        private const int SheetPerRow = 4;
        /// <summary>輸入欄一行的高度（也是送出鈕、按住說話那顆的高度）。</summary>
        private const float InputHeight = 42f;
        /// <summary>打字列上下的留白（打字列高＝這個＋輸入欄高）。</summary>
        private const float ComposerPad = 14f;
        /// <summary>多行輸入最高長到幾行高（再多就在欄裡捲）。</summary>
        private const float InputMaxHeight = 120f;
        private LayoutElement _composerHeight;
        private LayoutElement _slotHeight;
        private bool _recording;
        private Msg _photoBubble;
        private Msg _voiceBubble;
        private float _nextTick;
        private bool _busy;
        private bool _syncing;
        private bool _retried;
        private bool _sawDone;
        private string _pendingTurn = "";
        private string _currentTurn = "";
        private string _lastGood = "";
        private string _lastSent = "";
        // ── 延時聚合（owner 2026-11-04）────────────────────────────────────────
        // 「用戶的前台文字／圖片／表情包發送，設計延時聚合發送」：連發幾筆先攢著，視窗關閉才合成一輪。
        // 觀感上他還是立刻看到自己那幾顆泡泡，只是她**一次**回一輪（人不會你發三句就分別回三次）。
        // ⚠️ 語意（owner 2026-11-04 當面糾正）：**他還在打字就不要送**。
        //   窗口是「輸入靜止多久」，不是「按下送出後多久」；送出只排隊，不阻塞輸入。
        private const float AggWindow = 3.0f;      // 停止輸入／送出後要靜多久才真的送出（owner 定 3s）
        private const float AggMax = 8.0f;         // 從第一筆排隊起算的硬上限（一直打不停也要送）
        private readonly List<JObject> _aggItems = new List<JObject>();
        private float _lastTypingAt;               // 最近一次在輸入框裡動字的時刻
        private float _lastQueueAt;                // 最近一次排進佇列（送出貼圖／照片也算）
        private float _aggStart;
        private bool _aggPumping;
        private int _aggEpoch;                       // 換角色／登出後，舊聚合泵不准再送新會話的內容
        /// <summary>這一份畫面現在是哪個角色的對話（換角色時整份狀態歸零，見 `ResetForEntity`）。</summary>
        private string _entityId = "";
        /// <summary>
        /// 每一輪一張票（替哪個角色、第幾號）：回呼只認**自己那張**還是不是現在這一張（unity-review U-2）。
        /// 以前共用一個可變的 `_turnEntity`：A 送出→換 B→B 送出，A 晚一幀的收尾看到的已經是 B，
        /// 於是 A 的「斷線」寫進 B 的佔位泡泡、B 的忙碌旗標被提前清掉。
        /// </summary>
        private readonly TurnTickets _tickets = new TurnTickets();
        /// <summary>有界未讀窗口：切頁、切角色與 App 重啟都保留；完整歷史仍以伺服器為準。</summary>
        private readonly ConversationStore _conversations =
            new ConversationStore(NextConfig.LoadConversationStore());
        /// <summary>只有正在顯示這一頁才直接標已讀；同角色在設定頁／往後的 Feed 也必須計未讀。</summary>
        private bool _chatVisible;
        public int UnreadTotal { get { return _conversations.TotalUnread(); } }
        public int UnreadFor(string entity) { return _conversations.Unread(entity); }
        private readonly Dictionary<string, Component> _pillLabels = new Dictionary<string, Component>();
        private readonly Dictionary<string, string> _pillNames = new Dictionary<string, string>();
        /// <summary>
        /// 這一頁的初始歷史還在路上。這期間**不准**補拿插隊：補拿會先把「有 id 沒文字」的回合
        /// 記進去重表，歷史回來時整批被當成「已顯示」跳掉 ⇒ 切過去只剩一顆「補」泡泡（實測踩到）。
        /// </summary>
        private bool _loadingHistory;

        // ── 捲動與更早的歷史（owner 2026-10-30）────────────────────────────────
        /// <summary>新訊息進來時使用者不在最底下（在往上翻）⇒ 不拉他下去，改浮一顆「新訊息」。</summary>
        private Button _newPill;
        /// <summary>畫面上最舊的那一輪（往上捲到頂時，從它之前再拿一頁）。</summary>
        private string _oldestTurn = "";
        private bool _hasOlder;
        private bool _loadingOlder;
        /// <summary>往上補更早的歷史時：新加的訊息不准把畫面拉到底、也不浮「新訊息」。</summary>
        private bool _quietAppend;
        /// <summary>最後一則訊息是哪一天（換日就插一條日期分隔）。</summary>
        private string _lastDayKey = "";
        /// <summary>剛送出的那一則（送不出去時連同錯誤泡泡一起收掉、重送）。</summary>
        private Msg _lastMine;
        /// <summary>按住說話時往上拖到取消區了（放開就丟掉，不留著送）。</summary>
        private bool _holdCancel;
        private const int OlderPage = 30;

        /// <summary>最後一個完整收到的 turn id（契約 v2 的重連游標）。</summary>
        public string LastGoodTurn { get { return _lastGood; } }

        // ── 回覆分段揭示（P7-①）：與舊殼同一支 `BlueAgent.Logic.ReplyReveal` ─────────
        /// <summary>這一輪的分段器；每條最多 3 句、依語音長度決定什麼時候 pop 下一條。</summary>
        private ReplyReveal _reveal = new ReplyReveal();
        /// <summary>這一輪目前為止的全文（delta 累積）。</summary>
        private string _streamText = "";
        /// <summary>這一輪已經畫出來的氣泡（與 `_reveal.Bubbles()` 對齊）。</summary>
        /// <summary>這一輪已經收到幾條 server 的 `part`。</summary>
        private int _serverParts;
        private readonly System.Collections.Generic.List<Msg> _liveRows =
            new System.Collections.Generic.List<Msg>();

        public void Build(RectTransform body, NextShell shell)
        {
            _shell = shell;
            // Unity 的 Destroy 要等幀尾才觸發舊 NextHold.OnDisable；不能先清旗標等它收尾，
            // 否則切角色時 HoldUp 會直接 return，舊麥克風仍開著。重建前先由舊畫面明確停錄。
            if (_recording) AbortRecording();
            _voiceBubble = null;
            // 換角色 ⇒ 這是**另一份對話**：訊息、去重表、游標、串流狀態一律歸零再重讀。
            // （owner 2026-10-27：切角色後畫面還停在 mira 的歷史 ⇒ 就是這裡沒歸零，
            //   `LoadRecent` 又被 `_messages.Count > 0` 擋掉。）
            if (_entityId != shell.EntityId) ResetForEntity(shell.EntityId);

            // 群聊强制单栏、无舞台，消息区从顶栏后直接撑满；1:1 保留既有大屏双栏。
            var group = ChatRepository.IsGroupKey(shell.EntityId);
            // 主人↔客人的私聊（P-C2）跟群一樣：沒有舞台、單欄（對方不是角色，沒有立繪可畫）
            var noStage = group || shell.IsPersonChat;
            var twoPane = NextLayout.TwoPane(shell.CanvasWidth) && !noStage;
            RectTransform column;
            RectTransform side;
            if (twoPane)
            {
                var panes = UiBuilder.Row("ChatPanes", body, 14f, new RectOffset(12, 12, 8, 8), TextAnchor.UpperLeft);
                UiBuilder.Weight(panes, 1f);
                side = UiBuilder.Column("ChatSide", panes, 6f, null, true, TextAnchor.UpperLeft);
                Share(side, 2f);
                column = UiBuilder.Column("Chat", panes, 6f, null, true, TextAnchor.UpperLeft);
                Share(column, 3f);
            }
            else
            {
                column = UiBuilder.Column("Chat", body, 6f, new RectOffset(10, 10, 8, 8), true, TextAnchor.UpperLeft);
                UiBuilder.Weight(column, 1f);
                side = column;
            }

            // 頁首（返回＋名字）在最上面、舞台之下才是她（設計稿 v5；以前返回鍵夾在舞台下、畫面中段）。
            // M1 從會話列表進來時保留返回；聊天本體、舞台與三條重畫路徑仍是原本這一份。
            var head = UiBuilder.Row("ChatHead", side, 8f, new RectOffset(2, 2, 0, 0), TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(head, NextConfig.ChatList ? 36f : 28f);
            if (NextConfig.ChatList)
            {
                var back = NextUi.IconButton("ChatListBack", head, "chevron-left", NextUi.Style.Navy, 32f);
                back.onClick.AddListener(shell.ShowChatList);
            }
            NextUi.Accent("ChatAccent", head, 5f, 18f);
            // 群名由列表注入；1:1 角色名仍按界面语言显示。
            var title = UiBuilder.Text("ChatTitle", head, shell.ConversationDisplayName,
                                       16f, NextTheme.Ink, TextAlignmentOptions.MidlineLeft, noWrap: true, bold: true);
            NextUi.Italic(title);
            var hintRow = UiBuilder.Row("ChatHintRow", head, 4f, null, TextAnchor.MiddleRight);
            UiBuilder.Weight(hintRow, 1f);
            UiBuilder.Icon("ChatHintIcon", "pulse", hintRow, 12f, NextTheme.Sky);
            _hint = UiBuilder.Text("ChatHint", hintRow, "", NextTheme.NoteSize, NextTheme.InkSoft,
                                   TextAlignmentOptions.MidlineRight, noWrap: true);

            StageAvatar stage = null;
            if (!noStage)
            {
                try { stage = NextStageView.Build(side, shell.EntityId, AvatarTimeline.Normal, NextConfig.BackgroundFor(shell.EntityId), twoPane,
                                                  shell, shell.EntityId, NextStageView.ChatStageHeight); }
                catch (Exception error) { Debug.LogWarning("[next] 立繪載入失敗：" + error.Message); }
            }
            shell.Stage = stage;
            if (!noStage && !twoPane) NextStageView.Frame(side.Find("Stage") as RectTransform);

            // 群内不再显示另一排角色切换；点各条消息的作者头像即可辨认说话者。
            var entities = shell.Entities;
            _pillLabels.Clear();
            _pillNames.Clear();
            // 1:1 也不再放角色切換（owner 2026-11-07）：換人一律回會話列表。
            // 只有關掉會話列表的回退模式（`NextConfig.ChatList=false`）沒有別的入口，才保留這排。
            if (!group && !NextConfig.ChatList && entities != null && entities.Length() > 1)
            {
                var picker = UiBuilder.Row("ChatEntityPicker", side, 6f, new RectOffset(2, 2, 0, 0), TextAnchor.MiddleLeft);
                UiBuilder.FixedHeight(picker, 40f);
                for (var i = 0; i < entities.Length(); i++)
                {
                    var row = entities.OptJObject(i);
                    if (row == null) continue;
                    var id = row.OptString("id");
                    var backend = row.OptString("name");
                    if (string.IsNullOrEmpty(backend)) backend = id;
                    var shown = shell.DisplayNameOf(id);
                    var selected = id == shell.EntityId;
                    var chip = EntityChip(picker, id, shown, selected);
                    var target = id;
                    chip.onClick.AddListener(() => shell.SwitchEntity(target, backend));
                    _pillNames[id] = shown;
                }
                RefreshUnread();
            }

            // 訊息區（可捲；新訊息貼底）
            RectTransform content;
            content = NextUi.ScrollArea("ChatStream", column, 10f, new RectOffset(2, 2, 18, 14), out _scroll);
            _list = content;
            // 上下邊緣淡出，不是硬切：捲動時氣泡在舞台下緣／輸入列上緣漸隱（owner 2026-11-07）。
            // 內距同時加大，停著不捲時最上、最下那一則不會落在淡出帶裡。
            var viewportMask = _scroll.viewport != null ? _scroll.viewport.GetComponent<RectMask2D>() : null;
            if (viewportMask != null) viewportMask.softness = new Vector2Int(0, 22);
            // 捲到頂 ⇒ 拿更早的一頁；回到底 ⇒ 收起「新訊息」
            _scroll.onValueChanged.AddListener(_ => OnScrolled());
            BuildNewPill((RectTransform)_scroll.transform);

            // 打字區：白色底條＋「+」、輸入欄、錄音、送出（黃色主鈕）
            // 兩側的鈕貼底（owner 2026-10-30）：長文把欄位撐高時，「+」／笑臉／送出跟最後一行對齊，不浮在中間
            var composer = UiBuilder.Row("ChatComposer", column, 6f, new RectOffset(8, 8, 6, 6), TextAnchor.LowerCenter);
            _composerHeight = UiBuilder.FixedHeight(composer, ComposerPad + InputHeight);
            var bar = UiBuilder.Backdrop("ChatComposerBg", composer, NextTheme.Composer, radius: 14f);
            var drop = bar.gameObject.AddComponent<UnityEngine.UI.Shadow>();
            drop.effectColor = NextTheme.Shadow;
            drop.effectDistance = new Vector2(0f, -2f);

            var plus = NextUi.IconButton("ChatAlbum", composer, "plus", NextUi.Style.Ghost, NextTheme.TapSize, round: true);
            plus.onClick.AddListener(ToggleSheet);
            _plusIcon = plus.transform.Find("ChatAlbumIcon") as RectTransform;

            // 語音／打字切換（2026-10-29 owner）：麥克風跟「+」同一邊、同一種圓鈕、不帶字；
            // 按了切到語音模式 ⇒ 輸入欄換成「按住說話」，麥克風換成鍵盤（按它回打字）
            _micToggle = NextUi.IconButton("ChatMic", composer, "mic", NextUi.Style.Ghost, NextTheme.TapSize, round: true);
            _micToggle.onClick.AddListener(() => SetVoiceMode(true));
            _keyboardToggle = NextUi.IconButton("ChatKeyboard", composer, "keyboard", NextUi.Style.Ghost, NextTheme.TapSize, round: true);
            _keyboardToggle.onClick.AddListener(() => SetVoiceMode(false));

            var slot = UiBuilder.Row("ChatInputSlot", composer, 0f, null, TextAnchor.MiddleLeft);
            UiBuilder.Weight(slot, 1f);
            _slotHeight = UiBuilder.FixedHeight(slot, InputHeight);
            // 寬只吃剩下的（不看字的 preferred 寬）：不然長文一打，欄位變寬、把「送信」擠扁
            _slotHeight.minWidth = 0f;
            _slotHeight.preferredWidth = 0f;
            _input = NextUi.Field("ChatInput", slot, NextStrings.Get("input_hint"), InputHeight, InputHeight / 2f);
            UiBuilder.Weight((RectTransform)_input.transform, 1f);
            // 多行（owner 2026-10-30）：Return 換行、按「送信」才送；欄位跟著字長高，最多 `InputMaxHeight`
            _input.lineType = TMP_InputField.LineType.MultiLineNewline;
            _input.textComponent.textWrappingMode = TextWrappingModes.Normal;   // 欄位建出來是單行不換行
            // 游標（owner 2026-10-30）：就地編輯在 `UiBuilder.Input` 統一開；這裡只換聊天欄自己的樣子
            _input.onFocusSelectAll = false;                                    // 點進來是放游標，不是全選
            _input.caretColor = NextTheme.Sky;
            _input.selectionColor = new Color(NextTheme.Sky.r, NextTheme.Sky.g, NextTheme.Sky.b, 0.25f);
            _input.onValueChanged.AddListener(_ =>
            {
                FitInput();
                _lastTypingAt = Time.unscaledTime;      // 還在打 ⇒ 聚合窗口一直往後延（owner 2026-11-04）
            });
            _input.onSelect.AddListener(_ =>                          // 要打字了 ⇒ 選單／表情盤收起來（QQ 同）
            {
                CloseSheet();
                _stickers.Close();
            });
            BuildHold(slot);

            // 表情包（2026-10-29）：笑臉打開表情盤（跟「+」選單同一個位置，開一個就收另一個）
            var smiley = NextUi.IconButton("ChatSticker", composer, "smiley", NextUi.Style.Ghost, NextTheme.TapSize, round: true);
            smiley.onClick.AddListener(ToggleStickers);

            var send = NextUi.Button("ChatSend", composer, NextStrings.Get("send"), NextUi.Style.Accent, InputHeight, 0f, "send");
            send.onClick.AddListener(OnSendPressed);

            BuildSheet(column, shell);
            _stickers.Build(column, shell, SendSticker);
            _pendingWav = null;
            SetVoiceMode(false);

            Refresh();
            SetHint(DefaultHint());
            UiBuilder.Settle((RectTransform)body);
            SetChatVisible(true);
            LoadRecent();
            // 同角色離頁期間收到的交付：已有記憶體歷史時 LoadRecent 會直接返回，這裡補上已讀交接。
            if (!_loadingHistory) ShowStashed();
            PaintLiveParts();            // 端側「說到一半」的那一輪先畫上去（正式列到了會換掉）
        }

        /// <summary>頁首右側的常駐說明：群聊的規則、或 1:1 的聲音狀態。</summary>
        private string DefaultHint()
        {
            if (ChatRepository.IsGroupKey(_entityId)) return NextStrings.Get("chat_hint_group");
            if (_shell == null) return "";
            if (_shell.IsPersonChat) return NextStrings.Get("chat_hint_person");
            return _shell.IsMaster
                ? NextStrings.Get(_shell.WantVoice ? "chat_hint_master_voice" : "chat_hint_master_mute")
                : NextStrings.Get("chat_hint_guest");
        }

        public void SetChatVisible(bool visible)
        {
            _chatVisible = visible;
            if (visible) RefreshUnread();
        }

        private void PersistConversations()
        {
            NextConfig.SaveConversationStore(_conversations.Snapshot());
        }

        /// <summary>
        /// S3：照片先沿用舊上傳取得 media_id，再與文字／貼圖一起寫本地 Outbox。
        /// 不再由這條路直接呼叫 /api/image 起一輪；Agent 的媒體理解仍是後續遷移邊界。
        /// </summary>
        public void SendPickedImage(byte[] bytes, string note)
        {
            if (bytes == null || bytes.Length == 0) return;
            // 延時聚合（owner 2026-11-04）：先只上傳拿 media_id，照片跟文字／貼圖一起合成一輪。
            SetHint(NextStrings.Get("photo_sending"));
            var entity = _shell.EntityId;
            _shell.Api.UploadMedia(bytes, entity, json =>
            {
                if (!string.IsNullOrEmpty(entity) && entity != _shell.EntityId) return;   // 上傳途中換了角色
                var id = json != null ? json.OptString("id") : "";
                if (string.IsNullOrEmpty(id))
                {
                    SetHint(NextStrings.Get("photo_sending"));
                    return;
                }
                EnqueueItem(new JObject().Put("kind", "photo").Put("media_id", id)
                                         .Put("text", note ?? ""));
            }, failure =>
            {
                Debug.LogWarning("[next] 上傳照片失敗：" + failure);
                SetHint(NextStrings.StreamFailure(failure));
            });
        }

        /// <summary>作廢目前這段對話的延時聚合；舊協程醒來後只會自行退出。</summary>
        public void CancelPendingAggregation()
        {
            _aggEpoch++;
            _aggItems.Clear();
            _aggPumping = false;
        }

        /// <summary>
        /// 換角色 ⇒ 把「屬於某個角色」的狀態全部歸零：訊息清單、去重表、游標（改成新角色自己的）、
        /// 串流/分段器、忙碌旗標。畫面上的泡泡由 `ClearBody()` 重建，這裡只負責資料。
        /// 游標改讀 `NextConfig.OwnTurnFor(entity)`：每個角色各有一顆，不會把 A 的游標拿去補 B 的回合。
        /// </summary>
        private void ResetForEntity(string entity)
        {
            CancelPendingAggregation();
            _messages.Clear();
            _shownTurns.Clear();
            _liveRows.Clear();
            _reveal.Reset();
            _streamText = "";
            _serverParts = 0;
            _pendingTurn = "";
            _currentTurn = "";
            _lastSent = "";
            _busy = false;
            _syncing = false;
            _retried = false;
            _sawDone = false;
            _sendingVoice = false;
            _recording = false;
            _photoBubble = null;
            _voiceBubble = null;
            // 在途那一輪作廢＋取消：它晚到的 frame／closed 認不出現在的票，什麼都不動
            _tickets.Invalidate();
            if (_shell != null && _shell.Api != null) _shell.Api.CancelTurn();
            _loadingHistory = false;
            _oldestTurn = "";
            _hasOlder = false;
            _loadingOlder = false;
            _quietAppend = false;
            _lastDayKey = "";
            _lastMine = null;
            _entityId = entity;
            _lastGood = NextConfig.OwnTurnFor(entity);
            Debug.Log("[next] 對話歸零 entity=" + entity +
                      " cursor=" + (string.IsNullOrEmpty(_lastGood) ? "-" : _lastGood));
        }

        /// <summary>S3：先畫 SQLite 裡的完整本地副本，再在背景同步；網路回包不直接變成泡泡。</summary>
        private void LoadRecent()
        {
            if (_shell == null || _messages.Count > 0 || _shell.Api.Chat == null) return;
            var entity = _entityId;
            if (!ChatRepository.IsGroupKey(entity))
                _shell.Api.Chat.EnsureConversation(entity, _shell.ChatAccountId, _shell.Me?.OptString("display") ?? "");
            PaintLocalLatest(entity);
            _loadingHistory = false;               // 本地已可顯示；事件可在背景同步期間安全合併。
            _shell.Api.ChatSync.Kick();
            _shell.Api.ChatSync.SyncConversation(entity, (ok, error) =>
            {
                if (entity != _entityId) return;
                if (ok) PaintLocalLatest(entity);
                else if (_messages.Count == 0) SetHint(NextStrings.F("history_fail", "reason", error));
                ShowStashed();
            });
        }

        private void PaintLocalLatest(string entity)
        {
            if (_shell?.Api.Chat == null || entity != _entityId) return;
            var items = _shell.Api.Chat.Messages(entity, 50);
            PaintHistory(entity, LocalTurns(items, _shell.ChatAccountId));
            if (!_chatVisible) return;
            var conversation = _shell.Api.Chat.Conversation(entity);
            for (var i = items.Count - 1; i >= 0; i--)
            {
                var id = items[i].Json.OptString("id");
                if (items[i].Pending || id.StartsWith("local_", StringComparison.Ordinal)) continue;
                _shell.Api.ChatSync.MarkRead(conversation?.OptString("id"), id);
                break;
            }
            if (_conversations.Unread(entity) > 0)
            {
                _conversations.Take(entity);
                PersistConversations();
                RefreshUnread();
            }
        }

        private static JArray LocalTurns(List<ChatItem> items, string principal)
        {
            var turns = new JArray();
            for (var i = 0; items != null && i < items.Count; i++)
                turns.Put(LocalTurn(items[i], principal));
            return turns;
        }

        /// <summary>把 App message/part 適配成既有氣泡、貼圖、日期與延時聚合渲染器的輸入。</summary>
        private static JObject LocalTurn(ChatItem item, string principal)
        {
            var message = item.Json;
            var id = message.OptString("id");
            var at = message.OptDouble("created_at");
            var author = message.OptJObject("author")?.OptString("id") ?? "";
            var parts = message.OptJArray("parts");
            // 收回的訊息是墓碑（App Server 2026-11-16）：內容清空、只留位置 ⇒ 置中一行「已收回」，不畫空泡泡
            if (message.OptBoolean("deleted"))
                return new JObject().Put("turn_id", id).Put("at", at).Put("kind", "recalled")
                    .Put("text", NextStrings.Get("message_deleted"));
            // 她打來你沒接（App Server 寫的 `kind=call` 部件，2026-11-17）⇒ 置中一行「不在着信」，不畫空泡泡
            for (var i = 0; parts != null && i < parts.Length(); i++)
            {
                if (parts.OptJObject(i)?.OptString("kind") == "call")
                    return new JObject().Put("turn_id", id).Put("at", at).Put("kind", "missed-call")
                        .Put("text", NextStrings.Get("chat_missed_call"));
            }
            var row = new JObject().Put("turn_id", id).Put("at", at).Put("pending", item.Pending)
                .Put("author_id", author);
            if (author == principal)
            {
                var texts = new List<string>();
                for (var i = 0; parts != null && i < parts.Length(); i++)
                {
                    var text = parts.OptJObject(i)?.OptString("text") ?? "";
                    if (!string.IsNullOrEmpty(text)) texts.Add(text);
                }
                row.Put("asked", string.Join("\n", texts.ToArray())).Put("asked_parts", parts);
                return row;
            }
            var bubbles = new JArray();
            var photos = new JArray();
            var stickers = new JArray();
            for (var i = 0; parts != null && i < parts.Length(); i++)
            {
                var part = parts.OptJObject(i);
                if (part == null) continue;
                var text = part.OptString("text");
                if (!string.IsNullOrEmpty(text)) bubbles.Put(text);
                if (part.OptString("kind") == "photo")
                {
                    var url = part.OptString("url");
                    if (string.IsNullOrEmpty(url)) url = "/v1/media/" + part.OptString("media_id") + "?w=360";
                    photos.Put(new JObject().Put("url", url));
                }
                if (part.OptString("kind") == "sticker")
                {
                    var url = part.OptString("url");
                    if (string.IsNullOrEmpty(url)) url = NextStickers.UrlOf(author, part.OptString("sticker"));
                    stickers.Put(new JObject().Put("url", url));
                }
            }
            row.Put("parts", bubbles).Put("text", bubbles.Length() == 1 ? bubbles.OptString(0) : "")
                .Put("photos", photos).Put("stickers", stickers);
            return row;
        }

        /// <summary>把一窗歷史畫上去（畫過的 turn 跳過）；回新畫了幾輪。</summary>
        private int PaintHistory(string entity, JArray turns)
        {
            var added = RenderTurns(entity, turns, true);
            // 最舊的那一輪：往上捲到頂時從它之前再拿（第一次載入才定；之後的補拿都是更新的）
            if (string.IsNullOrEmpty(_oldestTurn) && turns != null && turns.Length() > 0)
            {
                _oldestTurn = turns.OptJObject(0)?.OptString("turn_id") ?? "";
                _hasOlder = turns.Length() >= 50;                    // 一頁 50：少於一頁＝已經到頭
            }
            // 0 件不是故障：這一頁只顯示**這一端**（手機）的回合，Discord 上說過的不進來
            // （owner 2026-10-27「App 與 DC 各自獨立」）⇒ 明講，免得被當成壞掉。
            var shown = _shownTurns.Count;
            // 有歷史就回到這一頁的說明（群聊「一輪由一位回應」等），不再顯示「50 條記錄」這種除錯數字
            SetHint(shown > 0 ? DefaultHint() : NextStrings.Get("history_empty"));
            // 取值證用：切角色後 logcat 看得到「這一頁載的是誰、載了幾輪」
            Debug.Log("[next] 履歴 entity=" + entity + " loaded=" + added + " total=" + _messages.Count);
            if (added > 0 && !_quietAppend) FollowBottom(true);
            return added;
        }

        /// <summary>
        /// 一列一列畫（畫過的 turn 跳過）；回新畫了幾輪。<paramref name="cursor"/>＝要不要推進補拿游標
        /// （往上補更早的歷史時不推：游標是「最新收到哪一輪」）。
        /// </summary>
        private int RenderTurns(string entity, JArray turns, bool cursor)
        {
            var added = 0;
            for (var i = 0; turns != null && i < turns.Length(); i++)
            {
                var row = turns.OptJObject(i);
                if (row == null) continue;
                var id = row.OptString("turn_id");
                DropProvisional(id);                 // 正式那一列到了 ⇒ 先收掉暫時畫的分句
                if (!string.IsNullOrEmpty(id) && _shownTurns.Contains(id)) continue;
                var at = row.OptDouble("at");
                var asked = row.OptString("asked");
                // 送表情的那一輪：畫圖，不畫「（スタンプ：…）」那串字
                var askedSticker = row.OptString("asked_sticker");
                if (row.OptString("kind") == "call-log")
                {
                    // 通話結束的標記：置中小字，不是她的泡泡（通話內容本來就不進紀錄）
                    AppendMarker(CallMarkerText(row), id, at);
                }
                else if (row.OptString("kind") == "recalled" || row.OptString("kind") == "missed-call")
                {
                    AppendMarker(row.OptString("text"), id, at);   // 收回的那一則／她打來你沒接
                }
                else
                {
                    // 延時聚合：他那一輪可能有好幾筆（文字／照片／表情）⇒ 按當時的形狀一條一條畫；
                    // 舊資料沒有 `asked_parts` 就照舊（單張表情 → 單條文字）。
                    if (!AppendAskedParts(row, id, at))
                    {
                        if (!string.IsNullOrEmpty(askedSticker)) AppendSticker(NextStickers.UrlOf(entity, askedSticker), true, id, at);
                        else if (!string.IsNullOrEmpty(asked)) Append(asked, true, "", at).TurnId = id;
                    }
                    var authorName = ChatRepository.IsGroupKey(entity)
                        ? _shell.DisplayNameOf(row.OptString("author_id")) : "";
                    AppendHerParts(row, id, at, authorName);
                    AppendHerPhotos(row, id);
                    AppendHerStickers(row, id);
                }
                if (string.IsNullOrEmpty(id)) continue;
                _shownTurns.Add(id);
                added++;
                if (!cursor) continue;
                _lastGood = id;
                NextConfig.SetLastTurnFor(entity, id);
            }
            return added;
        }

        // ── 捲動：跟著最新、浮「新訊息」、往上補更早的歷史（owner 2026-10-30）────────────

        /// <summary>
        /// 新東西進來時要不要把畫面拉到底：本來就在底（或 <paramref name="force"/>：自己送出、剛載入）才拉；
        /// 使用者往上翻著看舊的 ⇒ 不拉，浮一顆「新訊息」讓他自己決定（以前一律拉到底，翻到一半被拽下去）。
        /// </summary>
        private void FollowBottom(bool force = false)
        {
            if (_scroll == null || _quietAppend) return;
            if (force || NearBottom())
            {
                NextUi.StickToBottom(_scroll);
                ShowNewPill(false);
            }
            else
            {
                ShowNewPill(true);
            }
        }

        /// <summary>現在是不是在最底下（量的是**這一刻**的版面：新的一列還沒排進去，量到的是加之前的位置）。</summary>
        private bool NearBottom()
        {
            if (_scroll == null || _scroll.content == null || _scroll.viewport == null) return true;
            var hidden = _scroll.content.rect.height - _scroll.viewport.rect.height;
            if (hidden <= 1f) return true;
            return hidden - _scroll.content.anchoredPosition.y <= 24f;
        }

        private bool NearTop()
        {
            if (_scroll == null || _scroll.content == null || _scroll.viewport == null) return false;
            var hidden = _scroll.content.rect.height - _scroll.viewport.rect.height;
            return hidden > 1f && _scroll.content.anchoredPosition.y <= 24f;
        }

        private void OnScrolled()
        {
            if (NearBottom()) ShowNewPill(false);
        }

        /// <summary>
        /// 每幀看一次：捲到頂、而且**手指已經放開** ⇒ 拿更早的一頁。
        /// ⚠️ 不能在拖動中插：`ScrollRect` 拖動時每幀用「開始拖的位置＋手指位移」直接設 content 的位置，
        /// 插完補的位移會被蓋掉，畫面一下跳走（模擬器實測）。
        /// </summary>
        private void MaybeLoadOlder()
        {
            if (!_hasOlder || _loadingOlder || _loadingHistory || _messages.Count == 0) return;
            if (Input.touchCount > 0 || Input.GetMouseButton(0)) return;
            if (NearTop()) LoadOlder();
        }

        /// <summary>對話區下緣那顆「新訊息」：點了拉到底。</summary>
        private void BuildNewPill(RectTransform host)
        {
            var rect = UiBuilder.NewRect("ChatNewPill", host);
            rect.gameObject.AddComponent<LayoutElement>().ignoreLayout = true;
            rect.anchorMin = rect.anchorMax = new Vector2(0.5f, 0f);
            rect.pivot = new Vector2(0.5f, 0f);
            rect.anchoredPosition = new Vector2(0f, 10f);
            var row = rect.gameObject.AddComponent<HorizontalLayoutGroup>();
            row.padding = new RectOffset(14, 16, 7, 7);
            row.spacing = 6f;
            row.childAlignment = TextAnchor.MiddleCenter;
            row.childControlWidth = true;
            row.childControlHeight = true;
            row.childForceExpandWidth = false;
            row.childForceExpandHeight = false;
            var fit = rect.gameObject.AddComponent<ContentSizeFitter>();
            fit.horizontalFit = ContentSizeFitter.FitMode.PreferredSize;
            fit.verticalFit = ContentSizeFitter.FitMode.PreferredSize;
            var plate = UiBuilder.Backdrop("ChatNewPillBg", rect, NextTheme.Sky, radius: 16f);
            var image = plate.GetComponent<Image>();
            image.raycastTarget = true;
            plate.gameObject.AddComponent<Shadow>().effectColor = NextTheme.Shadow;
            UiBuilder.Icon("ChatNewPillIcon", "chevron-down", rect, 14f, NextTheme.OnColor);
            UiBuilder.Text("ChatNewPillText", rect, NextStrings.Get("new_messages"), NextTheme.NoteSize,
                           NextTheme.OnColor, TextAlignmentOptions.Center, noWrap: true, bold: true);
            _newPill = rect.gameObject.AddComponent<Button>();
            _newPill.targetGraphic = image;
            _newPill.transition = Selectable.Transition.None;
            _newPill.onClick.AddListener(() => FollowBottom(true));
            rect.gameObject.AddComponent<NextPress>();
            rect.gameObject.SetActive(false);
        }

        private void ShowNewPill(bool show)
        {
            if (_newPill != null && _newPill.gameObject.activeSelf != show) _newPill.gameObject.SetActive(show);
        }

        /// <summary>捲到頂：只查本地 SQLite 的下一頁；背景同步已負責把服務端完整歷史補齊。</summary>
        private void LoadOlder()
        {
            if (!_hasOlder || _loadingOlder || _shell?.Api.Chat == null || string.IsNullOrEmpty(_oldestTurn)) return;
            _loadingOlder = true;
            var entity = _entityId;
            SetHint(NextStrings.Get("history_older_loading"));
            var items = _shell.Api.Chat.Messages(entity, OlderPage, _oldestTurn);
            var turns = LocalTurns(items, _shell.ChatAccountId);
            _loadingOlder = false;
            if (turns.Length() < OlderPage) _hasOlder = false;
            var added = turns.Length() > 0 ? PrependHistory(entity, turns) : 0;
            if (added == 0) _hasOlder = false;
            SetHint(_hasOlder ? DefaultHint() : NextStrings.Get("history_no_older"));
        }

        /// <summary>把更早的一頁插到最上面，畫面停在原來看的那一則（不跳）。</summary>
        private int PrependHistory(string entity, JArray turns)
        {
            var content = _scroll != null ? _scroll.content : null;
            // 錨點＝原本最上面那一則（不用日期分隔：接縫那條可能被拿掉）；插完讓它回到原來在視窗裡的位置
            var anchor = _messages.Find(m => string.IsNullOrEmpty(m.DayKey) && m.Row != null)?.Row;
            if (_scroll != null) _scroll.StopMovement();               // 慣性還在跑的話會把補的位移帶走
            Canvas.ForceUpdateCanvases();
            var anchorY = anchor != null && _scroll.viewport != null
                ? _scroll.viewport.InverseTransformPoint(anchor.position).y : 0f;
            var start = _messages.Count;
            var latestDay = _lastDayKey;
            _lastDayKey = "";                                        // 這一批自己從第一天開始插日期
            _quietAppend = true;
            int added;
            try { added = RenderTurns(entity, turns, false); }
            finally { _quietAppend = false; }
            var batchDay = _lastDayKey;
            _lastDayKey = latestDay;

            var fresh = _messages.GetRange(start, _messages.Count - start);
            _messages.RemoveRange(start, fresh.Count);
            _messages.InsertRange(0, fresh);
            for (var i = 0; i < fresh.Count; i++)
                if (fresh[i].Row != null) fresh[i].Row.SetSiblingIndex(i);
            // 接縫：原本最上面那條日期分隔如果跟這一批最後一天同一天，就多餘了
            if (fresh.Count > 0 && _messages.Count > fresh.Count)
            {
                var seam = _messages[fresh.Count];
                if (!string.IsNullOrEmpty(seam.DayKey) && seam.DayKey == batchDay) DropMessage(seam);
            }
            var first = turns.OptJObject(0)?.OptString("turn_id");
            if (!string.IsNullOrEmpty(first)) _oldestTurn = first;

            if (content != null && anchor != null)
            {
                KeepAnchor(anchor, anchorY);
                // 新的泡泡有些尺寸要到下一幀才定（量字寬、貼圖）⇒ 下一幀再對一次
                _shell.StartCoroutine(KeepAnchorNextFrame(anchor, anchorY));
            }
            return added;
        }

        /// <summary>讓 <paramref name="anchor"/> 回到視窗裡原來的高度（content 整片上下挪）。</summary>
        private void KeepAnchor(RectTransform anchor, float anchorY)
        {
            if (anchor == null || _scroll == null || _scroll.content == null || _scroll.viewport == null) return;
            LayoutRebuilder.ForceRebuildLayoutImmediate(_scroll.content);
            var now = _scroll.viewport.InverseTransformPoint(anchor.position).y;
            _scroll.content.anchoredPosition += new Vector2(0f, anchorY - now);
        }

        private System.Collections.IEnumerator KeepAnchorNextFrame(RectTransform anchor, float anchorY)
        {
            yield return null;
            KeepAnchor(anchor, anchorY);
        }

        // ── 時間與日期分隔（owner 2026-10-30）────────────────────────────────────

        private static double Now()
        {
            return DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
        }

        private static DateTime LocalTime(double at)
        {
            return DateTimeOffset.FromUnixTimeMilliseconds((long)(at * 1000.0)).ToLocalTime().DateTime;
        }

        private static string DayKeyOf(double at)
        {
            return LocalTime(at).ToString("yyyy-MM-dd");
        }

        /// <summary>「今日」「昨日」，其餘「10月29日」（界面語言）。</summary>
        private static string DayLabel(double at)
        {
            var day = LocalTime(at).Date;
            var today = DateTime.Now.Date;
            if (day == today) return NextStrings.Get("day_today");
            if (day == today.AddDays(-1)) return NextStrings.Get("day_yesterday");
            return NextStrings.F("day_label", "m", day.Month, "d", day.Day);
        }

        /// <summary>這一則跟上一則不是同一天 ⇒ 先插一條日期分隔（置中小字）。</summary>
        private void MaybeDaySeparator(double at)
        {
            if (at <= 0) return;
            var key = DayKeyOf(at);
            if (key == _lastDayKey) return;
            _lastDayKey = key;
            var msg = new Msg { Mine = false, Text = DayLabel(at), Marker = true, DayKey = key, At = at };
            _messages.Add(msg);
            msg.Row = BuildMarker(msg.Text);
            msg.Label = FindLabel(msg.Row);
        }

        // ── 長按複製、送不出去重送（owner 2026-10-30）────────────────────────────

        /// <summary>長按一顆泡泡 ⇒ 把字複製起來（以前完全沒辦法拿她說的話去別的地方用）。</summary>
        // ── 收回（2026-11-16）：長按自己送達的那一則 ⇒ 泡泡下面一條「複製／收回」────────────────

        /// <summary>目前打開的操作條（一次只開一條）。</summary>
        private RectTransform _actions;

        /// <summary>
        /// 長按：自己說的、而且伺服器已經收下（有正式 id）⇒ 開操作條；她說的、還在送的 ⇒ 照舊直接複製。
        /// 還在送的不給收回：伺服器還沒有那一則，收回會打到 404。
        /// </summary>
        private void OnLongPress(Msg msg)
        {
            if (msg == null) return;
            if (!msg.Mine || msg.Marker || string.IsNullOrEmpty(msg.TurnId) ||
                msg.TurnId.StartsWith("local_", StringComparison.Ordinal))
            {
                CopyMessage(msg);
                return;
            }
            ShowActions(msg);
        }

        private void CloseActions()
        {
            if (_actions != null) UnityEngine.Object.Destroy(_actions.gameObject);
            _actions = null;
        }

        private void ShowActions(Msg msg)
        {
            CloseActions();
            if (_list == null || msg.Row == null) return;
            var bar = UiBuilder.Row("BubbleActions", _list, 8f, new RectOffset(0, 16, 0, 6),
                                    TextAnchor.MiddleRight);
            bar.SetSiblingIndex(msg.Row.GetSiblingIndex() + 1);
            _actions = bar;
            var copy = NextUi.Button("Copy", bar, NextStrings.Get("copy"), NextUi.Style.Secondary,
                                     30f, 0f, null, true, NextTheme.NoteSize);
            copy.onClick.AddListener(() => { CopyMessage(msg); CloseActions(); });
            var recall = NextUi.Button("Recall", bar, NextStrings.Get("recall"), NextUi.Style.Danger,
                                       30f, 0f, null, true, NextTheme.NoteSize);
            // 收回不能一按就生效（與公共區刪帖同一套）：第一下變「再按一次」，3 秒內第二下才送
            var armedUntil = 0f;
            recall.onClick.AddListener(() =>
            {
                if (Time.unscaledTime > armedUntil)
                {
                    armedUntil = Time.unscaledTime + 3f;
                    NextUi.SetText(NextUi.LabelOf(recall), NextStrings.Get("recall_confirm"));
                    return;
                }
                recall.interactable = false;
                Recall(msg, recall);
            });
            // 不動它就自己收起來（不留一條懸在對話裡的按鈕）
            NextMotion.Play(bar, 8f, _ => { }, 0f, null, () => { if (_actions == bar) CloseActions(); });
        }

        private void Recall(Msg msg, Button button)
        {
            var conversationId = _shell?.Api.Chat?.Conversation(_entityId)?.OptString("id") ?? "";
            if (string.IsNullOrEmpty(conversationId))
            {
                button.interactable = true;
                _shell?.Status(NextStrings.F("recall_fail", "reason", "conversation"));
                return;
            }
            var entity = _entityId;
            var messageId = msg.TurnId;
            var path = "/v1/conversations/" + Uri.EscapeDataString(conversationId) +
                       "/messages/" + Uri.EscapeDataString(messageId);
            _shell.Api.Delete(path, reply =>
            {
                if (!reply.Ok)
                {
                    if (button != null) button.interactable = true;
                    _shell.Status(NextStrings.F("recall_fail", "reason", reply.UserMessage()));
                    return;
                }
                _shell.Api.Chat?.ApplyTombstone(reply.Json.OptJObject("message"));
                if (entity == _entityId) ReplaceWithMarker(messageId);
                CloseActions();
            });
        }

        /// <summary>伺服器推來的收回（自己在別台裝置收回的、或角色收回的）。</summary>
        public void OnChatMessageDeleted(JObject envelope)
        {
            var message = envelope?.OptJObject("payload")?.OptJObject("message");
            if (message == null) return;
            ReplaceWithMarker(message.OptString("id"));
        }

        /// <summary>
        /// 同一則訊息可能畫成好幾顆（文字／照片／貼圖各一顆）⇒ 全部拿掉，在第一顆的位置放一行「已收回」。
        /// 已經是那一行了（自己收回後又收到伺服器事件）就不動。
        /// </summary>
        private void ReplaceWithMarker(string messageId)
        {
            if (string.IsNullOrEmpty(messageId)) return;
            var first = -1;
            var sibling = -1;
            var at = 0.0;
            var already = false;
            for (var i = 0; i < _messages.Count; i++)
            {
                if (_messages[i].TurnId != messageId) continue;
                if (_messages[i].Marker) { already = true; continue; }
                if (first < 0)
                {
                    first = i;
                    at = _messages[i].At;
                    if (_messages[i].Row != null) sibling = _messages[i].Row.GetSiblingIndex();
                }
            }
            if (first < 0 || already) return;
            for (var i = _messages.Count - 1; i >= 0; i--)
            {
                if (_messages[i].TurnId == messageId && !_messages[i].Marker) DropMessage(_messages[i]);
            }
            var marker = new Msg { Mine = false, Marker = true, Text = NextStrings.Get("message_deleted"),
                                   TurnId = messageId, At = at };
            marker.Row = BuildMarker(marker.Text);
            if (marker.Row != null && sibling >= 0) marker.Row.SetSiblingIndex(sibling);
            marker.Label = FindLabel(marker.Row);
            _messages.Insert(Math.Min(first, _messages.Count), marker);
        }

        private void CopyMessage(Msg msg)
        {
            if (msg == null || string.IsNullOrEmpty(msg.Text)) return;
            GUIUtility.systemCopyBuffer = msg.Text;
            _shell.Status(NextStrings.Get("copied"));
        }

        /// <summary>
        /// 這一句沒送到伺服器（還沒 `accepted` 就斷了）⇒ 錯誤那顆泡泡變成「點一下重送」。
        /// 已經 `accepted` 的不給重送：伺服器可能已經在做，重送會變成兩輪（那種交給補拿）。
        /// </summary>
        private void OfferResend(Msg failed, Msg mine, string text)
        {
            if (failed == null || failed.Row == null || string.IsNullOrEmpty(text)) return;
            failed.Text = failed.Text + "\n" + NextStrings.Get("tap_to_resend");
            if (failed.Label != null) failed.Label.text = failed.Text;
            var button = failed.Row.gameObject.AddComponent<Button>();
            button.transition = Selectable.Transition.None;
            // 命中區＝泡泡底（她那一側第一張圖是頭像，不能拿 GetComponentInChildren 隨便抓）
            var plate = failed.Row.Find((failed.Mine ? "Mine" : "Hers") + "/BubbleBg")?.GetComponent<Image>();
            if (plate != null) { plate.raycastTarget = true; button.targetGraphic = plate; }
            button.onClick.AddListener(() =>
            {
                if (_busy) { SetHint(NextStrings.Get("wait_previous")); return; }
                DropMessage(failed);
                DropMessage(mine);
                Send(text);
            });
        }

        /// <summary>多行輸入：欄位跟著字長高（一行 <see cref="InputHeight"/>，最高 <see cref="InputMaxHeight"/>）。</summary>
        private void FitInput()
        {
            if (_input == null || _input.textComponent == null || _slotHeight == null) return;
            var text = _input.textComponent;
            var width = text.rectTransform.rect.width;
            if (width <= 1f) return;
            var body = string.IsNullOrEmpty(_input.text) ? " " : _input.text;
            var one = text.GetPreferredValues(" ", width, 0f).y;
            var need = text.GetPreferredValues(body, width, 0f).y;
            var height = Mathf.Clamp(InputHeight + (need - one), InputHeight, InputMaxHeight);
            if (Mathf.Abs(height - _slotHeight.preferredHeight) < 0.5f) return;
            _slotHeight.minHeight = _slotHeight.preferredHeight = height;
            var field = _input.GetComponent<LayoutElement>();
            if (field != null) field.minHeight = field.preferredHeight = height;
            if (_composerHeight != null) _composerHeight.minHeight = _composerHeight.preferredHeight = height + ComposerPad;
            var composer = _composerHeight != null ? _composerHeight.transform as RectTransform : null;
            if (composer != null && composer.parent is RectTransform column) LayoutRebuilder.MarkLayoutForRebuild(column);
        }

        /// <summary>兩欄按比例分寬（不看各自內容的 preferred 寬，不然內容多的那欄會越擠越寬）。</summary>
        private static void Share(RectTransform pane, float weight)
        {
            var element = pane.GetComponent<LayoutElement>() ?? pane.gameObject.AddComponent<LayoutElement>();
            element.minWidth = 0f;
            element.preferredWidth = 0f;
            element.flexibleWidth = weight;
            element.flexibleHeight = 1f;
        }

        private void SetHint(string text)
        {
            NextUi.SetText(_hint, string.IsNullOrEmpty(text) ? "" : text);
        }

        /// <summary>每幀叫（`NextShell.Update`）：錄音中更新秒數與提示（每 0.2 秒一次就夠）。</summary>
        public void Tick()
        {
            MaybeLoadOlder();
            // 分段揭示的節拍（沒有語音幀時假裝語音在走；有幀就交給幀推）
            if (_reveal != null && _reveal.Tick()) SyncLiveRows(_reveal.Bubbles());
            if (!_recording) return;
            if (Time.realtimeSinceStartup < _nextTick) return;
            _nextTick = Time.realtimeSinceStartup + 0.2f;
            PaintHold();
        }

        // ── 語音簡訊（B1）────────────────────────────────────────────────────────

        /// <summary>「按住說話」那顆：平常淡藍、錄音中紅、錄好待送淡天藍（字也跟著換）。</summary>
        private void BuildHold(RectTransform slot)
        {
            _hold = UiBuilder.Row("ChatHold", slot, 6f, new RectOffset(12, 12, 0, 0), TextAnchor.MiddleCenter);
            UiBuilder.Weight(_hold, 1f);
            UiBuilder.FixedHeight(_hold, InputHeight);
            var plate = UiBuilder.Overlay("ChatHoldBg", _hold);
            var image = plate.gameObject.AddComponent<Image>();
            UiBuilder.Round(image, InputHeight / 2f);
            image.raycastTarget = true;                                  // 整顆都是按住的範圍
            _holdPlate = plate.gameObject.AddComponent<NextGradient>();
            UiBuilder.Border(_hold, NextTheme.PanelLine, 1f, InputHeight / 2f);
            _holdIcon = (RawImage)UiBuilder.Icon("ChatHoldIcon", "mic", _hold, 16f, NextTheme.Navy);
            _holdLabel = UiBuilder.Text("ChatHoldText", _hold, NextStrings.Get("hold_to_talk"), NextTheme.LabelSize,
                                        NextTheme.Navy, TextAlignmentOptions.Center, noWrap: true, bold: true);
            var hold = _hold.gameObject.AddComponent<NextHold>();
            hold.Down = HoldDown;
            hold.Up = HoldUp;
            hold.Moved = HoldMoved;
            PaintHold();
        }

        /// <summary>切語音／打字。切回打字：錄到一半就停掉、錄好沒送的丟掉。</summary>
        private void SetVoiceMode(bool voice)
        {
            CloseSheet();
            // 語音訊息走 Agent 的 /api/audio ⇒ 群與人的私聊都不給
            var allowVoice = !ChatRepository.IsGroupKey(_entityId) && !(_shell != null && _shell.IsPersonChat);
            voice = voice && allowVoice;
            if (!voice && _recording) AbortRecording();
            if (!voice) _pendingWav = null;
            _voiceMode = voice;
            if (_input != null) _input.gameObject.SetActive(!voice);
            if (_hold != null) _hold.gameObject.SetActive(voice);
            if (_micToggle != null) _micToggle.gameObject.SetActive(allowVoice && !voice);
            if (_keyboardToggle != null) _keyboardToggle.gameObject.SetActive(allowVoice && voice);
            PaintHold();
        }

        private void HoldDown()
        {
            // 只有「切到語音模式＋使用者真的按下」才錄（2026-10-29 owner）。
            // master／guest 都可以語音輸入（accounts-and-clients.md §1：guest 只是不出聲），這裡不分身份。
            if (!_voiceMode) return;
            if (_shell == null || _shell.VoiceRecorder == null || _recording) return;
            if (_busy) { SetHint(NextStrings.Get("wait_previous")); return; }
            var voice = _shell.VoiceRecorder;
            if (!voice.BeginRecording())
            {
                SetHint(string.IsNullOrEmpty(voice.Why) ? NextStrings.Get("record_start_fail") : voice.Why);
                return;
            }
            _pendingWav = null;                                          // 再按住＝重錄
            _recording = true;
            _holdCancel = false;
            PaintHold();
        }

        /// <summary>
        /// 按著往上拖：超過 <see cref="HoldCancelDistance"/> 就進「放開＝取消」（owner 2026-10-30）。
        /// 以前錄錯只能先送出去，或切回打字把它丟掉。
        /// </summary>
        private void HoldMoved(float pixels)
        {
            if (!_recording || Screen.width <= 0) return;
            var design = pixels * (_shell.CanvasWidth / Screen.width);
            var cancel = design > HoldCancelDistance;
            if (cancel == _holdCancel) return;
            _holdCancel = cancel;
            PaintHold();
        }

        /// <summary>按住說話時往上拖多遠（設計單位）算「要取消」。</summary>
        private const float HoldCancelDistance = 70f;

        /// <summary>放開：停錄、留著等「送信」（不在這裡送）。太短／空的直接丟掉並說一聲。</summary>
        private void HoldUp()
        {
            if (!_recording) return;
            if (_holdCancel)
            {
                _holdCancel = false;
                AbortRecording();
                _pendingWav = null;
                SetHint(NextStrings.Get("hold_cancelled"));
                PaintHold();
                return;
            }
            _recording = false;
            var voice = _shell != null ? _shell.VoiceRecorder : null;
            float seconds = 0f;
            var wav = voice != null ? voice.Stop(out seconds) : null;
            if (wav == null || wav.Length <= 44)
                SetHint(voice != null && !string.IsNullOrEmpty(voice.Why) ? voice.Why : NextStrings.Get("record_empty"));
            else if (seconds < 0.4f)
                SetHint(NextStrings.Get("record_too_short"));
            else
            {
                _pendingWav = wav;
                _pendingSeconds = seconds;
            }
            PaintHold();
        }

        private void AbortRecording()
        {
            _recording = false;
            _holdCancel = false;
            var voice = _shell != null ? _shell.VoiceRecorder : null;
            if (voice != null) voice.Stop(out _);
        }

        private void PaintHold()
        {
            if (_hold == null) return;
            string text;
            Color top, bottom, ink;
            if (_recording && _holdCancel)
            {
                text = NextStrings.Get("hold_release_cancel");
                top = NextTheme.Danger; bottom = NextTheme.Danger; ink = NextTheme.OnColor;
            }
            else if (_recording)
            {
                var seconds = _shell != null && _shell.VoiceRecorder != null ? _shell.VoiceRecorder.Seconds : 0f;
                text = NextStrings.F("hold_recording", "seconds", seconds.ToString("0.0"));
                top = NextTheme.Pink; bottom = NextTheme.Danger; ink = NextTheme.OnColor;
            }
            else if (_pendingWav != null)
            {
                text = NextStrings.F("voice_ready", "seconds", _pendingSeconds.ToString("0.0"));
                top = NextTheme.SkyPale; bottom = NextTheme.SkyPale; ink = NextTheme.SkyDeep;
            }
            else
            {
                text = NextStrings.Get("hold_to_talk");
                top = NextTheme.Field; bottom = NextTheme.Field; ink = NextTheme.Navy;
            }
            NextUi.SetText(_holdLabel, text);
            NextUi.SetColor(_holdLabel, ink);
            if (_holdIcon != null) _holdIcon.color = ink;
            if (_holdPlate != null)
            {
                _holdPlate.Top = top;
                _holdPlate.Bottom = bottom;
                _holdPlate.GetComponent<Graphic>().SetVerticesDirty();
            }
        }

        /// <summary>「送信」：打字模式送字；語音模式送剛剛按住錄好的那一段（沒錄就提示先按住）。</summary>
        private void OnSendPressed()
        {
            if (!_voiceMode) { Send(_input != null ? _input.text : ""); return; }
            CloseSheet();
            if (_recording) return;                                      // 還按著：放開才算錄好
            if (_pendingWav == null) { SetHint(NextStrings.Get("voice_hold_first")); return; }
            if (_busy) { SetHint(NextStrings.Get("wait_previous")); return; }
            var wav = _pendingWav;
            var seconds = _pendingSeconds;
            // 錄音先留著：server 明確拒收（HTTP 4xx／5xx，這一輪根本沒開）時還能直接再按送信；
            // 收下了（accepted）或連線斷在半路（不知道 server 有沒有收到）就丟掉——見 OnClosed。

            _busy = true;
            _sawDone = false;
            _sendingVoice = true;
            _lastSent = "";
            _voiceBubble = Append("…", true, $"♪ {seconds:0.0}s");      // 字型沒有 emoji（🎤 會變豆腐）
            FollowBottom(true);
            SetHint(NextStrings.Get("voice_sending"));
            var ticket = _tickets.Begin(_shell.EntityId);
            _shell.SetStage(AvatarTimeline.Thinking);
            // `NextConfig.Language` 在這裡是語音辨識的 `X-Lang`（不是界面語言）
            _shell.Api.SendAudio(wav, ticket.Entity, NextConfig.Language, _shell.WantVoice,
                                 (name, data) => OnFrame(ticket, name, data), failure => OnClosed(ticket, failure));
        }

        // ── 送一句 ───────────────────────────────────────────────────────────────

        private void Send(string text)
        {
            CloseSheet();
            text = (text ?? "").Trim();
            if (string.IsNullOrEmpty(text)) return;
            if (_input != null) _input.text = "";
            // 先聚合；FlushAgg 落 SQLite 成功後才畫自己的泡泡，沒有網路假成功。
            EnqueueItem(new JObject().Put("kind", "text").Put("text", text));
            FollowBottom(true);
        }

        /// <summary>保留既有輸入靜止聚合；差別只在最終写入改为本地 Outbox。</summary>
        private void EnqueueItem(JObject item)
        {
            if (item == null) return;
            if (_aggItems.Count == 0) _aggStart = Time.unscaledTime;
            _aggItems.Add(item);
            _lastQueueAt = Time.unscaledTime;
            SetHint(NextStrings.Get("sending"));
            if (!_aggPumping && _shell != null) _shell.StartCoroutine(AggPump(_aggEpoch));
        }

        private System.Collections.IEnumerator AggPump(int epoch)
        {
            _aggPumping = true;
            while (epoch == _aggEpoch && _aggItems.Count > 0)
            {
                var idle = Time.unscaledTime - Mathf.Max(_lastQueueAt, _lastTypingAt);
                var overtime = Time.unscaledTime - _aggStart >= AggMax;
                if (idle < AggWindow && !overtime) { yield return null; continue; }
                if (_busy) { yield return null; continue; }   // 語音舊路徑進行中時仍等待。
                FlushAgg();
            }
            if (epoch == _aggEpoch) _aggPumping = false;
        }

        /// <summary>S3 唯一文本寫入點：本地 message＋Outbox 同事務，絕不再雙寫 /api/turn。</summary>
        private void FlushAgg()
        {
            var items = new JArray();
            for (var i = 0; i < _aggItems.Count; i++)
                if (_aggItems[i] != null) items.Put(_aggItems[i]);
            _aggItems.Clear();
            if (items.Length() == 0 || _shell?.Api.Chat == null) return;
            var mutation = Guid.NewGuid().ToString("N");
            var queued = _shell.Api.Chat.Enqueue(_entityId, _shell.ChatAccountId,
                _shell.Me?.OptString("display") ?? "", items, mutation, Now());
            var one = new JArray();
            one.Put(LocalTurn(queued, _shell.ChatAccountId));
            RenderTurns(_entityId, one, false);
            FollowBottom(true);
            SetHint(NextStrings.Get("sending"));
            _shell.Api.ChatSync.Kick();
        }

        private void SendSticker(string id)
        {
            if (string.IsNullOrEmpty(id)) return;
            _stickers.Close();
            EnqueueItem(new JObject().Put("kind", "sticker").Put("sticker", id));
        }

        private void OnFrame(TurnTicket ticket, string name, JObject data)
        {
            // 不是現在這一輪（換過角色／又送了新的一輪／登出）⇒ 這串回包不屬於這一頁，丟掉
            if (!_tickets.IsCurrent(ticket)) return;
            var payload = data.OptJObject("payload");
            switch (name)
            {
                case "image":
                    // ⚠️ 這一幀的 data 是**平的**（`{"id":…}`），沒有 `payload` 包裝——
                    // 直接讀 payload 會拿到 null，縮圖就永遠掛不上去（踩過）。
                    var media = payload?.OptString("id");
                    if (string.IsNullOrEmpty(media)) media = data.OptString("id");
                    if (!string.IsNullOrEmpty(media)) AttachPhoto(_photoBubble, media);
                    _shell.SetStage(AvatarTimeline.Thinking);
                    break;
                case "transcript":
                    {
                        // 只有 `/api/audio` 會有：先告訴使用者「我聽到的是這句」
                        var heard = payload != null ? payload.OptString("text") : data.OptString("text");
                        if (_voiceBubble != null && _voiceBubble.Label != null)
                        {
                            _voiceBubble.Text = heard;
                            _voiceBubble.Label.text = string.IsNullOrEmpty(heard) ? "…" : heard;
                        }
                        SetHint(NextStrings.F("heard", "text", heard));
                    }
                    break;
                case "accepted":
                    // 只有語音請求被 server 收下才清待送錄音；圖片／文字的 accepted 與它無關。
                    if (_sendingVoice)
                    {
                        _pendingWav = null;
                        PaintHold();
                    }
                    // 新的一輪回覆：分段器歸零（顯示過的段落不會被清掉——那些是上一輪的氣泡）
                    _reveal.Reset();
                    _liveRows.Clear();
                    _streamText = "";
                    _serverParts = 0;
                    _pendingTurn = payload != null ? payload.OptString("turn_id") : data.OptString("turn_id");
                    if (string.IsNullOrEmpty(_pendingTurn)) _pendingTurn = data.OptString("turn_id");
                    _currentTurn = _pendingTurn;
                    SetHint(NextStrings.Get("thinking"));
                    break;
                case "thinking":
                    SetHint(NextStrings.Get("thinking"));
                    break;
                case "memory":
                    SetHint(NextStrings.Get("recalling"));
                    break;
                case "tool":
                    SetHint(NextStrings.Get("working"));
                    break;
                case "part":
                    // server 切好的氣泡（與 TTS 同一個句柄）：照它顯示就好，端末不再自己分段
                    if (payload != null) ShowPart(payload.OptInt("index", _liveRows.Count),
                                                  payload.OptString("text"));
                    break;
                case "delta":
                    if (payload != null) AppendDelta(payload.OptString("text"));
                    SetHint(NextStrings.Get("replying"));
                    _shell.SetStage(AvatarTimeline.Speaking);
                    break;
                case "speech":
                    if (payload != null) _shell.PlaySpeech(payload.OptString("audio"));
                    _shell.SetStage(AvatarTimeline.Speaking);
                    break;
                case "motion":
                    // 後端 Kimodo 的動作（3D 模式才會播；2D 忽略）
                    _shell.PlayMotion(payload);
                    break;
                case "photo":
                    // 她發出來的圖（委任出圖，owner 2026-11-04）：跟文字回覆同一條路，只是畫成圖片泡泡。
                    if (payload != null)
                    {
                        if (_liveRows.Count == 0) RemoveEmptyPlaceholder();
                        AppendSticker(payload.OptString("url"), false);
                    }
                    break;
                case "sticker":
                    // 她回了一張表情（`sticker.send` 工具）。先收掉送出時放的「…」空泡泡：
                    // 表情排在它後面，之後的字就接不回那顆，「…」會一直卡在表情上面（2026-10-31 模擬器實測）
                    if (payload != null)
                    {
                        if (_liveRows.Count == 0) RemoveEmptyPlaceholder();
                        AppendSticker(payload.OptString("url"), false);
                    }
                    break;
                case "speech_error":
                    SetHint(NextStrings.F("voice_failed_reason", "reason",
                                          payload != null ? payload.OptString("reason") : ""));
                    break;
                case "interrupted":
                    SetHint(NextStrings.Get("interrupted"));
                    break;
                case "done":
                    if (payload != null) SealReply(payload.OptString("text"), ReadBubbles(payload));
                    else SealReply("");
                    MarkDone();
                    break;
                case "result":
                    {
                        // ⚠️ 契約回傳的是 `decision.thread`（不是 thread_id）；頂層沒有 thread。
                        var thread = data.OptString("thread");
                        if (string.IsNullOrEmpty(thread))
                        {
                            var decision = data.OptJObject("decision");
                            if (decision != null)
                            {
                                thread = decision.OptString("thread");
                                if (string.IsNullOrEmpty(thread)) thread = decision.OptString("thread_id");
                            }
                        }
                        if (!string.IsNullOrEmpty(thread)) _shell.NoteThread(thread);
                    }
                    break;
                case "error":
                    var problem = NextStrings.ErrorCode(NextText.StreamErrorCode(data));
                    SealReply(NextStrings.F("reply_error", "reason", problem));
                    SetHint(NextStrings.F("error_hint", "reason", problem));
                    break;
            }
        }

        /// <summary>`done` ⇒ 這一輪算「完整收到」：記游標（契約 v2）並停止 pending。</summary>
        private void MarkDone()
        {
            var turn = string.IsNullOrEmpty(_currentTurn) ? _pendingTurn : _currentTurn;
            if (!string.IsNullOrEmpty(turn))
            {
                _shownTurns.Add(turn);
                _lastGood = turn;
                NextConfig.SetLastTurnFor(_entityId, turn);
            }
            _pendingTurn = "";
            _currentTurn = "";
            _busy = false;
            _sawDone = true;
            _voiceBubble = null;
            _shell.SetStage(AvatarTimeline.Normal);
        }

        private void OnClosed(TurnTicket ticket, string failure)
        {
            // 不是現在這一輪的收尾（被換角色或新的一輪取消掉的）⇒ 什麼都不動
            if (!_tickets.IsCurrent(ticket)) return;
            // 沒收到 `done` 就結束（半路斷線／伺服器被切）⇒ 先把空佔位收掉，再走補拿。
            if (!_sawDone) RemoveEmptyPlaceholder();
            SettleVoiceAfterClose(failure);
            _shell.SetStage(AvatarTimeline.Normal);

            if (string.IsNullOrEmpty(failure))
            {
                _busy = false;
                SetHint(" ");
                SyncMissed();
                return;
            }

            // 半路失敗：把「她這一輪」那個空佔位泡泡收掉（不然畫面會留一顆「…」）
            RemoveEmptyPlaceholder();

            // 401 不在這裡判（以前用 `failure.Contains("401")`，錯誤原文裡出現 401 就被當成 token 失效）：
            // `NextApi` 依狀態碼統一收口，登入那一段結束後這個回呼根本不會來。

            // 429：退避重試**一次**（契約 §5）
            if (failure.Contains("429") && !_retried && !string.IsNullOrEmpty(_lastSent))
            {
                _retried = true;
                _busy = false;
                SetHint(NextStrings.Get("busy_retry"));
                _shell.StartCoroutine(RetryAfter(ticket, _lastSent, 2f));
                return;
            }

            _busy = false;
            // 原文（`audio transport: …`）是給開發者看的：記進 log，畫面上換成界面語言的一句話
            Debug.Log("[next] 這一輪斷了：" + failure);
            var shown = NextStrings.StreamFailure(failure);
            var neverAccepted = string.IsNullOrEmpty(_currentTurn) && string.IsNullOrEmpty(_pendingTurn);
            SealReply(NextStrings.F("connection_lost", "reason", shown));
            SetHint(shown);
            // 伺服器根本沒收下（沒 `accepted`）的文字 ⇒ 錯誤那顆變成「點一下重送」（owner 2026-10-30）
            if (neverAccepted && !string.IsNullOrEmpty(_lastSent) && _messages.Count > 0)
                OfferResend(_messages[_messages.Count - 1], _lastMine, _lastSent);
            // 半路斷線：伺服器那一側可能已經做完 ⇒ 用游標把答案補回來
            SyncMissed();
        }

        /// <summary>
        /// 語音這一輪收尾時，按「這段錄音還要不要留著重送」分三種：
        /// <list type="bullet">
        /// <item>server 收下了（`accepted` 已清掉錄音）⇒ 什麼都不用做；</item>
        /// <item>server **明確拒收**（HTTP 4xx／5xx：辨識服務掛了、太大、太頻繁）⇒ 這一輪沒開，錄音留著、
        ///   畫面上這次的泡泡拿掉——重送時在最新的位置重畫一顆（不會把新錄音的字寫進舊泡泡）；</item>
        /// <item>連線斷在半路（狀態 0）或其他 ⇒ **不知道** server 有沒有收到，錄音丟掉、泡泡留著，
        ///   交給 `SyncMissed` 補拿（留著的話使用者再按一次就是重複的一輪）。</item>
        /// </list>
        /// </summary>
        private void SettleVoiceAfterClose(string failure)
        {
            if (_sendingVoice && _pendingWav != null)
            {
                if (NextText.ServerRejected(failure)) DropMessage(_voiceBubble);
                else _pendingWav = null;
                PaintHold();
            }
            _voiceBubble = null;
            _sendingVoice = false;
        }

        /// <summary>把一顆已經畫出來的訊息拿掉（畫面與清單一起）。</summary>
        private void DropMessage(Msg msg)
        {
            if (msg == null) return;
            if (msg.Row != null) UnityEngine.Object.Destroy(msg.Row.gameObject);
            _messages.Remove(msg);
        }

        private System.Collections.IEnumerator RetryAfter(TurnTicket ticket, string text, float seconds)
        {
            yield return new WaitForSeconds(seconds);
            // 這兩秒裡換了角色／登出 ⇒ 不把上一個角色的那句送給新的角色
            if (!_tickets.IsCurrent(ticket)) yield break;
            Send(text);
        }

        /// <summary>契約 §3：`delivery` 先落有界未讀 store，成功後才交由目前畫面渲染。</summary>
        public void OnDelivery(JObject delivery)
        {
            var text = delivery.OptString("text");
            var from = delivery.OptString("entity");
            var turn = delivery.OptString("turn_id");
            var audio = delivery.OptString("audio");
            // 舊版文字幀可能沒有 entity：語義上就是目前這個角色，也走同一條離頁未讀流程。
            // 不能保留舊的直接 Append：設定頁的 _list 已銷毀時，那條路會再次撞空參照。
            if (string.IsNullOrEmpty(from)) from = _entityId;
            if (string.IsNullOrEmpty(from) || string.IsNullOrEmpty(text)) return;
            var route = _conversations.Accept(from, turn, text, delivery.OptDouble("at"),
                                              delivery.OptString("kind"), audio);
            if (route != ConversationStore.Route.Added) return;
            PersistConversations();                       // store-first：畫面稍後失敗也不影響已收資料

            if (_chatVisible && from == _entityId && !_loadingHistory)
                ShowStashed(true);                        // 當前聊天頁：交出即已讀，最新一則照舊播音
            else
                RefreshUnread();                          // 包括「同角色但不在聊天頁」，都保留未讀

            if (!Application.isFocused)
            {
                try { NativeShell.NotifyReminder(_shell.DisplayNameOf(from), text); }
                catch (Exception error) { Debug.LogWarning("notify: " + error.Message); }
            }
        }

        /// <summary>交出這個角色收著的推播；歷史已畫過的跳過，其餘依收到順序補上並標已讀。</summary>
        private void ShowStashed(bool playLatestAudio = false)
        {
            // 先預覽、全部 Append 成功才 Take：中途例外時，記憶體與磁碟快照都保留未讀；
            // 同進程重試會靠 _shownTurns 跳過已畫好的前綴，不會重複泡泡或提前清已讀。
            var rows = _conversations.Preview(_entityId);
            for (var i = 0; i < rows.Count; i++)
            {
                var item = rows[i];
                if (!string.IsNullOrEmpty(item.TurnId) && _shownTurns.Contains(item.TurnId)) continue;
                // 不標「自発」（owner 2026-11-17：App 不用區分她的話是不是自己先開口，跟一般回覆一樣畫）
                Append(item.Text, false);
                if (!string.IsNullOrEmpty(item.TurnId)) _shownTurns.Add(item.TurnId);
                if (playLatestAudio && i == rows.Count - 1 && !string.IsNullOrEmpty(item.Audio))
                    _shell.PlaySpeech(item.Audio);
            }
            if (rows.Count > 0)
            {
                _conversations.Take(_entityId);
                PersistConversations();
            }
            RefreshUnread();
        }

        /// <summary>切換藥丸上的未讀數（`アスター ●2`）。</summary>
        private void RefreshUnread()
        {
            _shell?.RefreshTopUnread();                  // 公共区頂欄的未讀數跟著同一份計數走
            foreach (var pair in _pillLabels)
            {
                if (pair.Value == null) continue;
                var count = _conversations.Unread(pair.Key);
                var name = _pillNames.TryGetValue(pair.Key, out var shown) ? shown : pair.Key;
                NextUi.SetText(pair.Value, count > 0 ? name + " ●" + count : name);
                NextUi.SetColor(pair.Value, count > 0 ? NextTheme.Pink : NextTheme.Ink);
            }
        }

        // ── 「+」的底部選單（2026-10-29 owner：學 QQ，不要直接彈一個新頁）─────────────

        /// <summary>
        /// 打字列下面的一塊：照片與單人通話。Agent 私有的日記、任務、棋局與 TRPG 不屬於社交平台。
        /// 平常高度 0；按「+」展開，打字列被頂上去，「+」轉 45° 變成「×」。
        /// </summary>
        private void BuildSheet(RectTransform column, NextShell shell)
        {
            _sheetOpen = false;
            _sheet = UiBuilder.Column("ChatSheet", column, 6f, new RectOffset(8, 8, 14, 8), true, TextAnchor.UpperCenter);
            _sheetHeight = UiBuilder.FixedHeight(_sheet, 0f);
            _sheet.gameObject.AddComponent<RectMask2D>();
            _sheetGroup = _sheet.gameObject.AddComponent<CanvasGroup>();
            _sheetGroup.alpha = 0f;
            _sheetGroup.interactable = false;
            _sheetGroup.blocksRaycasts = false;
            var bg = UiBuilder.Backdrop("ChatSheetBg", _sheet, NextTheme.Composer, radius: 14f);
            bg.gameObject.AddComponent<Shadow>().effectColor = NextTheme.Shadow;

            // 平台只保留社交能力；Agent 私有管理頁與 TRPG 不進手機 App。
            var items = new List<(string name, string icon, string label, UnityEngine.Events.UnityAction go, bool accent)>();
            void Item(string name, string icon, string label, UnityEngine.Events.UnityAction go, bool accent = false)
            {
                items.Add((name, icon, label, go, accent));
            }
            Item("SheetPhoto", "image", NextStrings.Get("sheet_photo"), shell.PickImage);
            if (!ChatRepository.IsGroupKey(shell.EntityId) && !shell.IsPersonChat && shell.IsMaster)
                Item("SheetCall", "phone", NextStrings.Get("sheet_call"), shell.ShowCall);
            LayoutSheet(items);
        }

        /// <summary>
        /// 把選單項目排成「一頁 <see cref="SheetRows"/> 排 × <see cref="SheetPerRow"/> 個」，放不下就往右多一頁。
        /// 只有一排就只開一排高（guest 的三個不必空一排）；多頁才畫圓點。
        /// </summary>
        private void LayoutSheet(List<(string name, string icon, string label, UnityEngine.Events.UnityAction go, bool accent)> items)
        {
            var perPage = SheetRows * SheetPerRow;
            var pager = NextPager.Create("ChatSheetPages", _sheet);
            for (var start = 0; start < items.Count; start += perPage)
            {
                var page = pager.AddPage("ChatSheetPage" + start / perPage, 8f);
                for (var rowStart = start; rowStart < Mathf.Min(start + perPage, items.Count); rowStart += SheetPerRow)
                {
                    var row = UiBuilder.Row("ChatSheetRow" + rowStart, page, 6f, null, TextAnchor.UpperCenter);
                    for (var slot = 0; slot < SheetPerRow; slot++)
                    {
                        var index = rowStart + slot;
                        if (index >= items.Count)
                        {
                            // 不滿一排：補空格，讓每一格照樣是四分之一寬（不然最後一排會被撐開）
                            UiBuilder.EqualWidth(UiBuilder.NewRect("ChatSheetGap" + index, row));
                            continue;
                        }
                        var (name, icon, label, go, accent) = items[index];
                        NextUi.SheetItem(name, row, icon, label, accent).onClick.AddListener(() =>
                        {
                            CloseSheet(true);
                            go();
                        });
                    }
                }
            }
            pager.BuildDots("ChatSheetDots", _sheet);
            var rows = Mathf.Clamp(Mathf.CeilToInt(items.Count / (float)SheetPerRow), 1, SheetRows);
            _sheetOpenHeight = SheetPadHeight + SheetRowHeight + (rows - 1) * SheetExtraRowHeight
                               + (pager.Pages.Count > 1 ? 14f : 0f);
        }

        private void ToggleSheet()
        {
            if (_sheetOpen) CloseSheet();
            else OpenSheet();
        }

        private void ToggleStickers()
        {
            CloseSheet(true);
            var wasAtBottom = NearBottom();
            _stickers.Toggle();
            // 表情盤開合也會讓對話區變高變矮：本來在底下就照樣貼底（不然最新那幾則被盤子擠出畫面）
            if (wasAtBottom && _shell != null) _shell.StartCoroutine(StickAfter(0.25f));
        }

        private System.Collections.IEnumerator StickAfter(float seconds)
        {
            yield return new WaitForSeconds(seconds);
            FollowBottom(true);
        }

        private void OpenSheet()
        {
            if (_sheet == null || _sheetOpen) return;
            _stickers.Close(true);
            _sheetOpen = true;
            AnimateSheet(true, false);
        }

        /// <summary>收起來。<paramref name="instant"/>＝要換頁了，不必播（頁面馬上就拆）。</summary>
        private void CloseSheet(bool instant = false)
        {
            if (_sheet == null || !_sheetOpen) return;
            _sheetOpen = false;
            AnimateSheet(false, instant);
        }

        private void AnimateSheet(bool open, bool instant)
        {
            _sheetGroup.interactable = open;
            _sheetGroup.blocksRaycasts = open;
            var from = _sheetHeight.preferredHeight;
            var to = open ? _sheetOpenHeight : 0f;
            var column = _sheet.parent as RectTransform;
            void Apply(float p)
            {
                var height = Mathf.Lerp(from, to, p);
                _sheetHeight.minHeight = height;
                _sheetHeight.preferredHeight = height;
                _sheetGroup.alpha = open ? p : 1f - p;
                if (_plusIcon != null) _plusIcon.localRotation = Quaternion.Euler(0f, 0f, open ? -45f * p : -45f * (1f - p));
                if (column != null) LayoutRebuilder.MarkLayoutForRebuild(column);
            }
            if (instant) { Apply(1f); return; }
            // 選單一開，對話區就變矮 ⇒ 開完再量會以為「不在底下」而浮出「新訊息」（全頁截圖抓到）。
            // ⇒ 開之前在底下，開完就照樣貼底。
            var wasAtBottom = NearBottom();
            NextMotion.Play(_sheet, 0.22f, Apply, 0f, NextMotion.OutCubic,
                            () => { if (open && wasAtBottom) FollowBottom(true); });
        }

        /// <summary>切換角色那一顆：圓頭像＋名字；選中＝天藍外圈＋深藍底條。</summary>
        private UnityEngine.UI.Button EntityChip(RectTransform parent, string id, string name, bool selected)
        {
            var chip = UiBuilder.Row("Entity_" + id, parent, 6f, new RectOffset(4, 10, 3, 3), TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(chip, 38f);
            UiBuilder.EqualWidth(chip);
            var plate = UiBuilder.Backdrop("Entity_" + id + "Bg", chip,
                                           selected ? NextTheme.Navy : new Color(1f, 1f, 1f, 0.85f), radius: 19f);
            // ⚠️ `Backdrop` 預設不吃點擊（它是背景）；這一顆的底板就是按鈕的命中區，要打開——
            //    沒打開時頭像／字也都不吃點擊 ⇒ 整顆點了沒反應（2026-10-29 改版後踩到）。
            plate.GetComponent<UnityEngine.UI.Image>().raycastTarget = true;
            NextUi.Avatar("Entity_" + id + "Face", chip, id, 30f, selected ? NextTheme.SkyTop : NextTheme.PanelLine);
            var label = UiBuilder.Text("Entity_" + id + "Text", chip, name, NextTheme.LabelSize,
                                       selected ? NextTheme.OnColor : NextTheme.Ink, TextAlignmentOptions.MidlineLeft,
                                       weight: 1f, noWrap: true, bold: true);
            if (!selected) _pillLabels[id] = label;
            var button = chip.gameObject.AddComponent<UnityEngine.UI.Button>();
            button.targetGraphic = plate.GetComponent<UnityEngine.UI.Image>();
            button.transition = UnityEngine.UI.Selectable.Transition.None;
            chip.gameObject.AddComponent<NextPress>();
            return button;
        }

        /// <summary>把一句「她的話」直接寫進聊天頁（例：她拒絕上桌的那一句）。</summary>
        public void AppendHerLine(string text)
        {
            var body = (text ?? "").Trim();
            if (body.Length == 0) return;
            Append(body, false, "TRPG");
        }

        // ── 斷線補拿（A1）───────────────────────────────────────────────────────────

        /// <summary>
        /// S3：重連後重試持久 Outbox，並用 App Server 游標補齊訊息。
        /// </summary>
        public void SyncMissed()
        {
            if (_shell?.Api.Chat == null) return;
            _shell.Api.ChatSync.Kick();
            var entity = _entityId;
            _shell.Api.ChatSync.SyncConversation(entity, (ok, error) =>
            {
                if (entity == _entityId && ok) PaintLocalLatest(entity);
            });
        }

        public void OnChatAccepted(string conversationId, string mutationId)
        {
            if (_shell?.Api.Chat == null) return;
            // Repository 已把 local_<mutation> 換成 server id；畫面也要收掉同一顆暫存泡泡，
            // 否則重讀時 server id 不在 _shownTurns，會在 optimistic bubble 後再畫一次。
            var localId = "local_" + (mutationId ?? "");
            _shownTurns.Remove(localId);
            for (var i = _messages.Count - 1; i >= 0; i--)
                if (_messages[i].TurnId == localId) DropMessage(_messages[i]);
            if (!_chatVisible) return;
            var current = _shell.Api.Chat.Conversation(_entityId);
            if (current?.OptString("id") == conversationId) PaintLocalLatest(_entityId);
        }

        /// <summary>SSE 已由 Shell 先落庫；這裡只決定刷新目前頁或累積未讀。</summary>
        public void OnChatMessage(JObject envelope)
        {
            var message = envelope?.OptJObject("payload")?.OptJObject("message");
            if (message == null || _shell?.Api.Chat == null) return;
            var conversationId = message.OptString("conversation_id");
            var conversation = _shell.Api.Chat.ConversationById(conversationId);
            if (conversation?.OptString("kind") == "group")
            {
                // 群路由只认 conversation id；作者可能同时有自己的 1:1，绝不能并入那条私聊。
                var group = ChatRepository.GroupKey(conversationId);
                if (_chatVisible && group == _entityId)
                {
                    PaintLocalLatest(group);
                    _shell.Api.ChatSync.MarkRead(conversationId, message.OptString("id"));
                }
                return;                 // 群未读在 ChatRepository（服务端摘要＋事件累加），不写旧 ConversationStore。
            }
            var entity = message.OptJObject("author")?.OptString("id") ?? "";
            if (entity == _shell.ChatAccountId || string.IsNullOrEmpty(entity))
            {
                var participants = conversation?.OptJArray("participants");
                entity = "";
                for (var i = 0; participants != null && i < participants.Length(); i++)
                {
                    var account = participants.OptString(i);
                    if (!string.IsNullOrEmpty(account) && account != _shell.ChatAccountId) entity = account;
                }
            }
            if (string.IsNullOrEmpty(entity)) return;
            if (_chatVisible && entity == _entityId)
            {
                PaintLocalLatest(entity);
                _conversations.Take(entity);
                PersistConversations();
                RefreshUnread();
                _shell.Api.ChatSync.MarkRead(conversationId, message.OptString("id"));
                return;
            }
            if (message.OptJObject("author")?.OptString("id") == _shell.ChatAccountId) return;
            var preview = PreviewParts(message.OptJArray("parts"));
            var route = _conversations.Accept(entity, message.OptString("id"), preview,
                message.OptDouble("created_at"), "chat", "");
            if (route == ConversationStore.Route.Added) PersistConversations();
            RefreshUnread();
            if (!Application.isFocused && route == ConversationStore.Route.Added)
            {
                try { NativeShell.NotifyReminder(_shell.DisplayNameOf(entity), preview); }
                catch (Exception error) { Debug.LogWarning("notify: " + error.Message); }
            }
        }

        private static string PreviewParts(JArray parts)
        {
            var texts = new List<string>();
            for (var i = 0; parts != null && i < parts.Length(); i++)
            {
                var part = parts.OptJObject(i);
                if (part == null) continue;
                var text = part.OptString("text");
                if (!string.IsNullOrEmpty(text)) texts.Add(text);
                else if (part.OptString("kind") == "sticker") texts.Add(NextStrings.Get("preview_sticker"));
                else if (part.OptString("kind") == "photo") texts.Add(NextStrings.Get("preview_photo"));
            }
            return string.Join(" ", texts.ToArray());
        }

        // 語音／圖片舊流程的暫留恢復器；S3 文本聊天不會呼叫它，後續媒體遷移時移除。
        private void LegacySyncMissed()
        {
            // 初始歷史還在路上就別插隊（見 `_loadingHistory`）；那批資料本來就會含最新的回合
            if (_syncing || _loadingHistory || _shell == null) return;
            // 第一次登入（還沒有游標）不補：契約說 `since` 給找不到的 id 會回最近 50 筆，
            // 那在正式服上等於把歷史整段倒進畫面。有游標才補。
            if (string.IsNullOrEmpty(_lastGood)) return;
            _syncing = true;
            var entity = _entityId;                      // 補拿要按角色：不然會把別人的回合倒進這一頁
            _shell.Api.GetTurnsSince(_lastGood, entity, NextConfig.HistorySource, reply =>
            {
                if (entity != _entityId) return;         // 補拿途中換了角色 ⇒ 這批不是這一頁的
                _syncing = false;
                if (reply.Status == 401)
                {
                    _shell.OnTokenInvalid();
                    return;
                }
                // 游標過期／伺服器沒開 outbox ⇒ 安靜跳過（不是使用者能處理的事）
                if (reply.Status == 503 || reply.Status == 404) return;
                if (!reply.Ok) return;

                var rows = reply.Json.OptJArray("turns");
                if (rows == null || rows.Length() == 0) return;
                var recovered = 0;
                for (var i = 0; i < rows.Length(); i++)
                {
                    var row = rows.OptJObject(i);
                    if (row == null) continue;
                    var turn = row.OptString("turn_id");
                    var text = row.OptString("text");
                    var ok = row.OptBoolean("ok", true);
                    if (!ok && string.IsNullOrEmpty(text)) continue;
                    DropProvisional(turn);               // 正式那一列到了 ⇒ 收掉暫時畫的分句
                    if (!string.IsNullOrEmpty(turn) && _shownTurns.Contains(turn))
                    {
                        // 內容已在畫面上：只補游標
                        _lastGood = turn;
                        NextConfig.SetLastTurnFor(_entityId, turn);
                        continue;
                    }
                    // 通話標記：置中小字，不是她補回來的話（複檢 2026-10-30：掛斷後的標記多半走這條進來）
                    var marker = row.OptString("kind") == "call-log";
                    if (marker) AppendMarker(CallMarkerText(row), turn, row.OptDouble("at"));
                    else
                    {
                        AppendHerParts(row, turn, row.OptDouble("at"), NextStrings.Get("tag_recovered"));
                        AppendHerPhotos(row, turn);
                        AppendHerStickers(row, turn);
                    }
                    if (!string.IsNullOrEmpty(turn))
                    {
                        _shownTurns.Add(turn);
                        _lastGood = turn;
                        NextConfig.SetLastTurnFor(_entityId, turn);
                        if (!marker) recovered++;               // 標記不算「補回來的話」
                        if (row.OptBoolean("audio")) RecoverAudio(turn);
                    }
                }
                if (recovered > 0)
                {
                    SetHint(NextStrings.F("recovered_count", "count", recovered));
                }
            });
        }

        /// <summary>
        /// 他自己發的那幾條（延時聚合的一輪可能有多筆）——依當時的形狀畫：文字／照片／表情包。
        /// 回 `true` 表示畫過了（呼叫端就不用再補單條）。
        /// </summary>
        private bool AppendAskedParts(JObject row, string turnId, double at)
        {
            var parts = row != null ? row.OptJArray("asked_parts") : null;
            if (parts == null || parts.Length() == 0) return false;
            for (var i = 0; i < parts.Length(); i++)
            {
                var part = parts.OptJObject(i);
                if (part == null) continue;
                var kind = part.OptString("kind");
                var text = part.OptString("text");
                if (kind == "photo")
                {
                    var id = part.OptString("media_id");
                    if (!string.IsNullOrEmpty(id)) AppendSticker("/v1/media/" + id + "?w=360", true, turnId, at);
                    if (!string.IsNullOrEmpty(text)) Append(text, true, "", at).TurnId = turnId;
                }
                else if (kind == "sticker")
                {
                    var sticker = part.OptString("sticker");
                    if (!string.IsNullOrEmpty(sticker)) AppendSticker(NextStickers.UrlOf(_entityId, sticker), true, turnId, at);
                    if (!string.IsNullOrEmpty(text)) Append(text, true, "", at).TurnId = turnId;
                }
                else if (!string.IsNullOrEmpty(text))
                {
                    Append(text, true, "", at).TurnId = turnId;
                }
            }
            return true;
        }

        /// <summary>
        /// 她那一輪在畫面上的分段：`parts`（跟 SSE 的 `part` 幀同一份切句）⇒ **一條一個氣泡**。
        /// 沒有 `parts`（2026-11-04 之前的舊列、單條回覆、主動訊息）⇒ 照舊一整條。
        /// 為什麼要這支：串流時她是「一條一條短訊息」出現，補拿／歷史如果只畫全文，
        /// 同一輪就變成一大條（owner 2026-11-04 指出的對齊問題）。
        /// </summary>
        private int AppendHerParts(JObject row, string turnId, double at, string note)
        {
            var authorId = row != null ? row.OptString("author_id") : "";
            var parts = row != null ? row.OptJArray("parts") : null;
            var drawn = 0;
            if (parts != null && parts.Length() > 1)
            {
                for (var i = 0; i < parts.Length(); i++)
                {
                    drawn += AppendTextWithMarks(parts.OptString(i), turnId, at, i == 0 ? note : "", authorId);
                }
                return drawn;
            }
            var text = row != null ? row.OptString("text") : "";
            if (string.IsNullOrEmpty(text)) return 0;
            return AppendTextWithMarks(text, turnId, at, note, authorId);
        }

        /// <summary>
        /// 把她的正文按**英文標記**拆開再畫：`[[sticker:&lt;id&gt;]]`＝貼圖、`[[photo:&lt;id&gt;]]`＝圖。
        /// owner 2026-11-04：「為什麼不用加英文的記號標注表情包，這樣也方便前台渲染」——
        /// 從此歷史只靠正文就畫得出來，不必再依賴 `stickers` 陣列；沒有標記就是純文字。
        /// </summary>
        private int AppendTextWithMarks(string text, string turnId, double at, string note = "", string authorId = "")
        {
            if (string.IsNullOrEmpty(text)) return 0;
            var added = 0;
            foreach (var piece in SplitMarks(text))
            {
                if (piece.Key == "text")
                {
                    if (string.IsNullOrEmpty(piece.Value)) continue;
                    Append(piece.Value, false, note, at, authorId).TurnId = turnId;
                    added++;
                }
                else if (piece.Key == "sticker")
                {
                    var stickerAuthor = string.IsNullOrEmpty(authorId) ? _entityId : authorId;
                    AppendSticker(NextStickers.UrlOf(stickerAuthor, piece.Value), false, turnId, at, authorId);
                    added++;
                }
                else if (piece.Key == "photo")
                {
                    AppendSticker("/v1/media/" + piece.Value, false, turnId, at, authorId);
                    added++;
                }
            }
            return added;
        }

        /// <summary>正文 → [(text|sticker|photo, 值)]：認 `[[sticker:&lt;id&gt;]]` 與 `[[photo:&lt;id&gt;]]`。</summary>
        private static System.Collections.Generic.List<KeyValuePair<string, string>> SplitMarks(string text)
        {
            var list = new System.Collections.Generic.List<KeyValuePair<string, string>>();
            if (string.IsNullOrEmpty(text)) return list;
            var rest = text;
            while (!string.IsNullOrEmpty(rest))
            {
                var stickerAt = rest.IndexOf("[[sticker:", StringComparison.Ordinal);
                var photoAt = rest.IndexOf("[[photo:", StringComparison.Ordinal);
                var start = stickerAt < 0 ? photoAt : (photoAt < 0 ? stickerAt : Math.Min(stickerAt, photoAt));
                if (start < 0) break;
                var end = rest.IndexOf("]]", start, StringComparison.Ordinal);
                if (end < 0) break;
                var head = rest.Substring(0, start).Trim();
                if (head.Length > 0) list.Add(new KeyValuePair<string, string>("text", head));
                var isSticker = stickerAt >= 0 && start == stickerAt;
                var open = isSticker ? "[[sticker:".Length : "[[photo:".Length;
                var id = rest.Substring(start + open, end - start - open).Trim();
                if (id.Length > 0) list.Add(new KeyValuePair<string, string>(isSticker ? "sticker" : "photo", id));
                rest = rest.Substring(end + 2);
            }
            var tail = (rest ?? "").Trim();
            if (tail.Length > 0) list.Add(new KeyValuePair<string, string>("text", tail));
            return list;
        }

        /// <summary>outbox 那一列裡她發出來的圖（`photos: [{id, url, name}]`，委任出圖）。</summary>
        private void AppendHerPhotos(JObject row, string turnId)
        {
            var authorId = row != null ? row.OptString("author_id") : "";
            var list = row != null ? row.OptJArray("photos") : null;
            for (var i = 0; list != null && i < list.Length(); i++)
            {
                var item = list.OptJObject(i);
                var url = item != null ? item.OptString("url") : "";
                if (string.IsNullOrEmpty(url)) continue;
                AppendSticker(url, false, turnId, 0, authorId);
            }
        }

        /// <summary>outbox 那一列裡她回的表情（`stickers: [{id, entity, url}]`）。</summary>
        private void AppendHerStickers(JObject row, string turnId)
        {
            // 正文已經有英文標記（`[[sticker:…]]`）就別再畫一次——那條路已經把圖畫出來了
            // （owner 2026-11-04：兩邊都畫會變成同一張貼圖出現兩次）。
            if (row != null && row.OptString("text").IndexOf("[[sticker:", StringComparison.Ordinal) >= 0) return;
            var authorId = row != null ? row.OptString("author_id") : "";
            var list = row != null ? row.OptJArray("stickers") : null;
            for (var i = 0; list != null && i < list.Length(); i++)
            {
                var item = list.OptJObject(i);
                if (item == null) continue;
                var url = item.OptString("url");
                if (string.IsNullOrEmpty(url)) url = NextStickers.UrlOf(item.OptString("entity"), item.OptString("id"));
                AppendSticker(url, false, turnId, row.OptDouble("at"), authorId);
            }
        }

        private void RecoverAudio(string turnId)
        {
            _shell.Api.GetTurnAudio(turnId, bytes =>
            {
                if (_shell.Voice != null && bytes != null && bytes.Length > 0)
                {
                    try { _shell.Voice.PlayWav(bytes, "recovered"); }
                    catch (System.Exception error) { Debug.LogWarning("[next] 補拿音檔播放失敗：" + error.Message); }
                }
            }, fail => Debug.Log("[next] 補拿音檔失敗：" + fail.Describe()));
        }

        // ── 訊息資料 ─────────────────────────────────────────────────────────────

        /// <param name="at">這一則的時間（epoch 秒）；0＝現在（即時收到的）。</param>
        private Msg Append(string text, bool mine, string note = "", double at = 0, string authorId = "")
        {
            if (at <= 0) at = Now();
            MaybeDaySeparator(at);
            var msg = new Msg { Mine = mine, Text = text ?? "", At = at,
                                AuthorId = authorId ?? "", Note = note ?? "" };
            _messages.Add(msg);
            var row = BuildBubble(msg, msg.Note);
            msg.Row = row;
            msg.Label = FindLabel(row);
            FollowBottom();
            return msg;
        }

        /// <summary>一顆表情包（我送的／她回的）：沒有泡泡底，就是那張圖。</summary>
        private Msg AppendSticker(string url, bool mine, string turnId = "", double at = 0, string authorId = "")
        {
            if (string.IsNullOrEmpty(url)) return null;
            if (at <= 0) at = Now();
            MaybeDaySeparator(at);
            var msg = new Msg { Mine = mine, Sticker = url, TurnId = turnId ?? "", At = at,
                                AuthorId = authorId ?? "" };
            _messages.Add(msg);
            msg.Row = BuildBubble(msg, "");
            FollowBottom();
            return msg;
        }

        /// <summary>給別的頁回來時留一句話（例：通話被掛斷的原因）。</summary>
        public void Notice(string text)
        {
            SetHint(text);
        }

        // ── 系統標記（例：通話結束）─────────────────────────────────────────────
        // 通話的**內容**不進對話紀錄（owner 2026-10-29），只留一行「發生過通話」。
        // 這一行不是她說的話 ⇒ 不給泡泡、不給頭像，置中小字淡色。

        private Msg AppendMarker(string text, string turnId, double at = 0)
        {
            if (at <= 0) at = Now();
            MaybeDaySeparator(at);
            var msg = new Msg { Mine = false, Text = text ?? "", Marker = true,
                                TurnId = turnId ?? "", At = at };
            _messages.Add(msg);
            msg.Row = BuildMarker(msg.Text);
            msg.Label = FindLabel(msg.Row);
            FollowBottom();
            return msg;
        }

        private RectTransform BuildMarker(string text)
        {
            // 頁面不在（被 `ClearBody()` 拆了／還沒建）⇒ 不建 UI，只留資料。
            // 沒有這一行：使用者停在設定頁時她主動說話 ⇒ `SetParent(已銷毀)` 拋
            // `MissingReferenceException` ⇒ 整批事件丟掉（2026-11-04 取證）。
            if (_list == null) return null;
            var row = UiBuilder.Row("Marker", _list, 0f, new RectOffset(30, 30, 2, 2),
                                    TextAnchor.MiddleCenter);
            // `noWrap: true`（UiBuilder 自己的旗標；不要去碰 TMP 的
            // `enableWordWrapping`，那個在這一版已標 obsolete ⇒ 編譯閘門會紅）
            UiBuilder.Text("MarkerText", row, text,
                           NextTheme.MarkerSize, NextTheme.MarkerInk,
                           TextAlignmentOptions.Center, noWrap: true);
            return row;
        }

        /// <summary>
        /// 通話標記的文字：伺服器有給 `call_s`（秒數）就按**界面語言**說
        /// （「通話 3分12秒」／「通话 3分12秒」）；舊資料沒有 ⇒ 用伺服器寫的那句。
        /// </summary>
        private static string CallMarkerText(JObject row)
        {
            var seconds = row != null ? row.OptInt("call_s") : 0;
            if (seconds <= 0) return row != null ? row.OptString("text") : "";
            if (seconds < 60) return NextStrings.F("call_marker_sec", "n", seconds);
            return NextStrings.F("call_marker_min", "m", seconds / 60, "s", seconds % 60);
        }

        /// <summary>把伺服器上那張圖掛進氣泡（P4b）：抓到才畫，抓不到留文字。</summary>
        private void AttachPhoto(Msg msg, string mediaId)
        {
            if (msg == null || msg.Row == null || string.IsNullOrEmpty(mediaId)) return;
            _shell.Api.GetMediaTexture(mediaId, texture =>
            {
                if (texture == null) return;
                if (msg.Row == null)
                {
                    UnityEngine.Object.Destroy(texture);   // 泡泡已經不在（換角色／換頁）⇒ 沒人要這張
                    return;
                }
                var host = new GameObject("Photo", typeof(RectTransform), typeof(UnityEngine.UI.Image));
                var rect = (RectTransform)host.transform;
                rect.SetParent(msg.Row, false);
                var image = host.GetComponent<UnityEngine.UI.Image>();
                image.preserveAspect = true;
                image.raycastTarget = false;
                NextUi.SetOwnedSprite(image, texture);
                var layout = host.AddComponent<UnityEngine.UI.LayoutElement>();
                layout.preferredHeight = 150f;
                layout.minHeight = 150f;
                FollowBottom();
            });
        }

        /// <summary>收掉最後一顆「她的」空泡泡（連線中途失敗時用）。</summary>
        private void RemoveEmptyPlaceholder()
        {
            if (_messages.Count == 0) return;
            var last = _messages[_messages.Count - 1];
            if (last.Mine || !string.IsNullOrEmpty(last.Text) || !string.IsNullOrEmpty(last.Sticker)) return;
            if (last.Row != null) UnityEngine.Object.Destroy(last.Row.gameObject);
            _messages.RemoveAt(_messages.Count - 1);
        }

        private void AppendDelta(string piece)
        {
            if (string.IsNullOrEmpty(piece)) return;
            _streamText += piece;
            // **兜底**：server 沒有給 `part` 幀時（舊 server／非串流）才在本機分段，
            // 有 part 幀就以 server 為準（一份實作）。
            if (_serverParts == 0 && _reveal.SetText(_streamText)) SyncLiveRows(_reveal.Bubbles());
            FollowBottom();
        }

        /// <summary>server 推來的一條氣泡：index 決定位置；缺的補、已有的更新（不刪、不重排）。</summary>
        private void ShowPart(int index, string text)
        {
            if (string.IsNullOrEmpty(text)) return;
            if (index < _liveRows.Count)
            {
                var row = _liveRows[index];
                row.Text = text;
                if (row.Label != null) row.Label.text = text;
            }
            else
            {
                _liveRows.Add(NewLiveRow(text));
            }
            _serverParts++;
            RememberLiveParts();          // 收到一句就落一句（owner 2026-11-04）
            FollowBottom();
        }

        /// <summary>
        /// 把「這一輪已經收到的分句」寫進端側（按角色一份）。App 被殺、斷網、切走再回來都還原得出來；
        /// 收尾（`MarkDone`）時清掉——那之後 outbox 的 `parts` 才是權威。
        /// ⚠️ 不以「語音聽完」為條件：句子一收到就落，跟語音合成／播放進度無關（owner 2026-11-04）。
        /// </summary>
        private void RememberLiveParts()
        {
            var turn = !string.IsNullOrEmpty(_currentTurn) ? _currentTurn : _pendingTurn;
            if (string.IsNullOrEmpty(_entityId) || string.IsNullOrEmpty(turn)) return;
            var parts = new System.Collections.Generic.List<string>();
            for (var i = 0; i < _liveRows.Count; i++)
            {
                var row = _liveRows[i];
                if (row != null && !string.IsNullOrEmpty(row.Text)) parts.Add(row.Text);
            }
            if (parts.Count == 0) return;
            var body = new JObject().Put("turn_id", turn).Put("at", Now());
            var arr = new JArray();
            for (var i = 0; i < parts.Count; i++) arr.Put(parts[i]);
            body.Put("parts", arr);
            NextConfig.SetLiveTurnFor(_entityId, body.ToString());
        }

        /// <summary>端側還有沒有「說到一半」的那一輪；有就先畫上去（正式那一列到了再換掉）。</summary>
        private void PaintLiveParts()
        {
            var raw = NextConfig.LiveTurnFor(_entityId);
            if (string.IsNullOrEmpty(raw)) return;
            JObject body;
            try { body = new JObject(raw); }
            catch (System.Exception)
            {
                NextConfig.SetLiveTurnFor(_entityId, "");
                return;
            }
            var turn = body.OptString("turn_id");
            var arr = body.OptJArray("parts");
            if (arr == null || arr.Length() == 0 || (!string.IsNullOrEmpty(turn) && _shownTurns.Contains(turn)))
            {
                NextConfig.SetLiveTurnFor(_entityId, "");
                return;
            }
            var at = body.OptDouble("at");
            var rows = new System.Collections.Generic.List<Msg>();
            for (var i = 0; i < arr.Length(); i++)
            {
                var piece = arr.OptString(i);
                if (string.IsNullOrEmpty(piece)) continue;
                var msg = Append(piece, false, "", at);
                msg.TurnId = turn;
                rows.Add(msg);
            }
            if (rows.Count == 0)
            {
                NextConfig.SetLiveTurnFor(_entityId, "");
                return;
            }
            _provisional[turn ?? ""] = rows;
        }

        /// <summary>
        /// 正式那一列到了 ⇒ 先把「暫時畫上去的分句」拿掉，免得同一個 turn 出現兩份。
        /// </summary>
        private void DropProvisional(string turnId)
        {
            System.Collections.Generic.List<Msg> rows;
            if (string.IsNullOrEmpty(turnId) || !_provisional.TryGetValue(turnId, out rows)) return;
            _provisional.Remove(turnId);
            for (var i = 0; i < rows.Count; i++)
            {
                var msg = rows[i];
                if (msg == null) continue;
                if (msg.Row != null) UnityEngine.Object.Destroy(msg.Row.gameObject);
                _messages.Remove(msg);
            }
            // 這一輪的權威版本已經在畫面上了 ⇒ 端側那份暫存可以清掉
            var live = NextConfig.LiveTurnFor(_entityId);
            if (!string.IsNullOrEmpty(live))
            {
                try
                {
                    if (new JObject(live).OptString("turn_id") == turnId) NextConfig.SetLiveTurnFor(_entityId, "");
                }
                catch (System.Exception) { NextConfig.SetLiveTurnFor(_entityId, ""); }
            }
        }

        /// <summary>
        /// 這一輪的下一顆氣泡。第一顆**沿用送出時先放的「…」空泡泡**——以前每次都新開一顆，
        /// 那顆「…」就一直留在畫面上（要重載歷史才消失）。
        /// </summary>
        private Msg NewLiveRow(string text)
        {
            var last = _messages.Count > 0 ? _messages[_messages.Count - 1] : null;
            if (_liveRows.Count == 0 && last != null && !last.Mine && !last.Marker
                && string.IsNullOrEmpty(last.Text) && string.IsNullOrEmpty(last.Sticker))
            {
                last.Text = text ?? "";
                if (last.Label != null) last.Label.text = last.Text;
                return last;
            }
            return Append(text, false);
        }

        /// <summary>把畫面上的氣泡對齊成 `bubbles`：少的補、多的只會長大（不刪、不重排）。</summary>
        private void SyncLiveRows(System.Collections.Generic.List<string> bubbles)
        {
            for (var i = 0; i < bubbles.Count; i++)
            {
                if (i >= _liveRows.Count)
                {
                    _liveRows.Add(NewLiveRow(bubbles[i]));
                    continue;
                }
                var row2 = _liveRows[i];
                row2.Text = bubbles[i];
                if (row2.Label != null) row2.Label.text = bubbles[i];
            }
            FollowBottom();
        }

        /// <summary>把 done 幀裡的 `bubbles` 讀成清單（server 的分段結果）。</summary>
        private static System.Collections.Generic.List<string> ReadBubbles(JObject payload)
        {
            var arr = payload != null ? payload.OptJArray("bubbles") : null;
            if (arr == null) return null;
            var list = new System.Collections.Generic.List<string>();
            for (var i = 0; i < arr.Length(); i++)
            {
                var item = arr.OptString(i);
                if (!string.IsNullOrEmpty(item)) list.Add(item);
            }
            return list.Count > 0 ? list : null;
        }

        private void SealReply(string finalText,
                              System.Collections.Generic.List<string> payloadBubbles = null)
        {
            if (!string.IsNullOrEmpty(finalText)) _streamText = finalText;
            var serverBubbles = payloadBubbles != null
                ? payloadBubbles
                : (_serverParts > 0 ? null : ReplySplit.Groups(_streamText, ReplySplit.SentencesPerBubble));
            if (serverBubbles != null && serverBubbles.Count > 0) SyncLiveRows(serverBubbles);
            var last = _liveRows.Count > 0 ? _liveRows[_liveRows.Count - 1]
                                           : (_messages.Count > 0 ? _messages[_messages.Count - 1] : null);
            if (last == null || last.Mine) last = Append("", false);
            last.TurnId = _currentTurn;
            if (last.Label != null) last.Label.text = string.IsNullOrEmpty(last.Text) ? "…" : last.Text;
            FollowBottom();
        }

        private void Refresh()
        {
            if (_list == null) return;
            for (var i = _list.childCount - 1; i >= 0; i--)
            {
                UnityEngine.Object.Destroy(_list.GetChild(i).gameObject);
            }
            foreach (var msg in _messages)
            {
                // 通話標記重畫時也要是置中小字（複檢 2026-10-30：以前回到聊天頁就變成她的泡泡）
                var row = msg.Marker ? BuildMarker(msg.Text) : BuildBubble(msg, msg.Note ?? "");
                msg.Row = row;
                msg.Label = FindLabel(row);
            }
            FollowBottom(true);
        }

        private RectTransform BuildBubble(Msg msg, string note)
        {
            // 頁面不在（`ClearBody()` 之後／還沒 `Build()`）⇒ 只留資料，不建 UI。
            // 資料已經進 `_messages`，回到聊天頁時 `Refresh()` 會整份重畫 ⇒ 不會漏訊息。
            if (_list == null) return null;
            var row = UiBuilder.Row("Msg", _list, 6f, null,
                                    msg.Mine ? TextAnchor.UpperRight : TextAnchor.UpperLeft);
            var authorId = string.IsNullOrEmpty(msg.AuthorId) ? _entityId : msg.AuthorId;
            if (!msg.Mine) NextBoardUi.Face("MsgFace", row, _shell, authorId, 28f, NextTheme.PanelLine);
            if (!string.IsNullOrEmpty(msg.Sticker))
            {
                NextStickers.Picture("MsgSticker", row, _shell.Api, msg.Sticker);
                return row;
            }
            // 群聊：作者名是氣泡**外**上方一行小字（設計稿 v5），不是氣泡裡一整條灰底標籤
            RectTransform holder = row;
            if (!msg.Mine && !string.IsNullOrEmpty(note) && ChatRepository.IsGroupKey(_entityId) &&
                note == _shell.DisplayNameOf(authorId))
            {
                holder = UiBuilder.Column("HersCol", row, 2f, null, false, TextAnchor.UpperLeft);
                // 氣泡寬要照自己的字寬（NextFitWidth），不能被這一欄撐到整列寬
                holder.GetComponent<VerticalLayoutGroup>().childForceExpandWidth = false;
                var holderLayout = holder.gameObject.AddComponent<LayoutElement>();
                holderLayout.flexibleWidth = 0f;
                UiBuilder.Text("BubbleAuthor", holder, note, NextTheme.TinySize, NextTheme.InkSoft,
                               TextAlignmentOptions.MidlineLeft, noWrap: true);
                note = "";
            }
            var bubble = UiBuilder.Column(msg.Mine ? "Mine" : "Hers", holder, 3f,
                                          new RectOffset(12, 12, 8, 8),
                                          false, TextAnchor.UpperLeft);
            UiBuilder.FixedWidth(bubble, msg.Mine ? 300f : 312f);
            var bg = UiBuilder.Backdrop("BubbleBg", bubble, msg.Mine ? NextTheme.MineBubble : NextTheme.HerBubble);
            var image = bg.GetComponent<UnityEngine.UI.Image>();
            if (msg.Mine) UiBuilder.RoundCorners(image, 12f, 3f, 12f, 12f);
            else UiBuilder.RoundCorners(image, 3f, 12f, 12f, 12f);
            var drop = bg.gameObject.AddComponent<UnityEngine.UI.Shadow>();
            drop.effectColor = NextTheme.Shadow;
            drop.effectDistance = new Vector2(0f, -2f);
            if (!string.IsNullOrEmpty(note))
            {
                var tag = NextUi.Badge("BubbleTag", bubble, note, new Color(1f, 1f, 1f, 0.22f), NextTheme.OnColor);
                tag.GetComponent<UnityEngine.UI.LayoutElement>().minHeight = 16f;
            }
            var body = UiBuilder.Text("BubbleText", bubble, string.IsNullOrEmpty(msg.Text) ? "…" : msg.Text,
                                      NextTheme.BodySize, NextTheme.OnColor, TextAlignmentOptions.TopLeft);
            if (!msg.Mine) NextFonts.UseHer(body);    // 她說的是日語：日語字形，不跟界面語言
            // 時間（owner 2026-10-30：以前看歷史分不出是哪天哪時說的）：泡泡右下角一行小字
            if (msg.At > 0)
            {
                var time = UiBuilder.Text("BubbleTime", bubble, LocalTime(msg.At).ToString("HH:mm"), NextTheme.TinySize,
                                          new Color(1f, 1f, 1f, 0.7f), TextAlignmentOptions.BottomRight, noWrap: true);
                if (time is TMP_Text stamp) stamp.raycastTarget = false;
            }
            // 長按複製（泡泡底要吃點擊才收得到；捲動照樣交給外面的 ScrollRect）
            image.raycastTarget = true;
            var press = bubble.gameObject.AddComponent<NextLongPress>();
            press.Fire = () => OnLongPress(msg);
            if (body is TMP_Text measured)
            {
                var fit = bubble.gameObject.AddComponent<NextFitWidth>();
                fit.Text = measured;
                fit.Max = msg.Mine ? 300f : 312f;
                fit.Min = string.IsNullOrEmpty(note) ? 48f : 84f;
            }
            return row;
        }

        /// <summary>
        /// 這一顆的**正文**那行字。⚠️ 不能拿「最後一個 TMP」：泡泡右下角加了時間之後，最後一個是時間
        /// ⇒ 串流的字被寫進那行單行小字、溢出到泡泡左邊，「…」也寫進時間裡（2026-10-31 模擬器實測）。
        /// </summary>
        private static TMP_Text FindLabel(RectTransform row)
        {
            // `BuildBubble`／`BuildMarker` 在頁面不在時回 null ⇒ 這裡要認得空的 row
            if (row == null) return null;
            var found = row.GetComponentsInChildren<TMP_Text>(true);
            foreach (var text in found)
            {
                if (text.name == "BubbleText" || text.name == "MarkerText") return text;
            }
            return found.Length > 0 ? found[found.Length - 1] : null;
        }

        /// <summary>按鈕裡第一顆文字。</summary>
        private static Component FindFirstText(Transform root)
        {
            var found = root.GetComponentsInChildren<TMP_Text>(true);
            return found.Length > 0 ? found[0] : null;
        }
    }
}
