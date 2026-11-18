using System;
using System.Collections;
using System.Collections.Generic;
using BlueAgent.Logic;
using BlueAgent.Screens;
using BlueAgent.Spike;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

namespace BlueAgent.Next
{
    /// <summary>
    /// **單人通話**（owner 2026-10-29：「實時和 agent 對話」、只給 master、可以打斷她）。
    ///
    /// 一通電話＝App Server 的 `/v1/calls` 會話；MR 不走這一頁，仍保留舊鏈路。
    /// **先響鈴、她接了才進語音**（owner 2026-11-17）：撥號那一個 POST 期間畫面是「呼び出し中…」，
    /// 伺服器問過她（Agent 的 `runtime/inner/call_pickup.py`）才回。不接 ⇒ 409 `call_declined`＋`reason`：
    /// `asleep` 響滿幾秒後「応答がありません」；`on_call` 馬上「話し中です」；`declined` 響一下後顯示
    /// 「いまは出られないみたい」與她那一句（同一句也會以私聊送到），停幾秒自動回聊天頁。
    /// 通話期間她的回覆一律出聲。端末這一側只做三件事：
    /// <list type="number">
    /// <item>一直聽：<see cref="MicCapture"/> → 16 kHz／100 ms PCM 小幀串行上傳；
    ///   **切句只在 App Server**，完整語音段由 brain STT／對話／TTS 後經會話 SSE 回來；</item>
    /// <item>打斷：服務端偵測開口 ⇒ `interrupt` 帶新 epoch，本機立即停播，舊世代幀一律丟；</item>
    /// <item>心跳：15 秒一次 `/v1/calls/&lt;id&gt;/ping`；410／404 就收線。</item>
    /// </list>
    /// 這一頁的物件被銷毀（換頁／登出／換角色）＝掛斷：<see cref="OnDestroy"/> 收麥、掛線，不留一通沒人聽的電話。
    /// </summary>
    public sealed class NextCallView : MonoBehaviour
    {
        private const float PingSeconds = 15f;
        //: 兩階段響鈴（2026-11-17）：她 0 ms 就接也要看得見響鈴，所以最少響 RingMinSeconds。
        private const float RingMinSeconds = 1.2f;
        private const float RingPollSeconds = 0.35f;
        private const float RingMaxSeconds = 20f;

        /// <summary>現在這一通（登出時殼要先叫它掛，見 <see cref="HangUpActive"/>）。</summary>
        private static NextCallView _active;
        /// <summary>已離頁但撥號／掛斷還在路上的一通；重撥必須等它收乾淨。</summary>
        private static NextCallView _retiring;

#if BA_SELFTEST
        /// <summary>驗收鉤子（`NextUiDump`）：拍通話頁時不開麥克風。</summary>
        public static bool NoMic;
#endif

        private enum Phase { Dialing, Declined, Live, Ended }

        /// <summary>沒人接（睡著）時至少響幾秒才放棄；她看了狀況拒接時響幾秒（立刻被掛像機器）。</summary>
        private const float NoAnswerRingSeconds = 6f;
        private const float DeclineRingSeconds = 2.5f;
        /// <summary>拒接畫面停多久再自動回聊天頁（有她一句話的時候給長一點，讀得完）。</summary>
        private const float DeclinedShowSeconds = 2.5f;
        private const float DeclinedLineShowSeconds = 4.5f;

        private NextShell _shell;
        private string _entity = "";
        private string _callId = "";
        private Phase _phase = Phase.Dialing;
        private float _dialedAt;
        private string _declinedText = "";
        private bool _dialRequestInFlight;
        private Action _afterRetire;
        private MicCapture _mic;
        private const int FrameSamples = 1600; // 16 kHz / 100 ms
        /// <summary>一次上傳帶幾幀（「像打電話」Phase 1：請求數 10/s → 2.5/s）。</summary>
        private const int FramesPerRequest = 4;
        /// <summary>上行佇列上限（幀）；超過就丟最舊的，不再整通掛斷。</summary>
        private const int MaxQueuedFrames = 200;
        private readonly List<short> _pending = new List<short>(FrameSamples);
        private readonly Queue<byte[]> _frames = new Queue<byte[]>();
        private bool _frameSending;
        private int _nextFrameSeq = 1;
        private int _eventCursor;
        private int _epoch;
        private float _levelDb = -90f;
        private Coroutine _callEvents;
        private float _nextEventsReconnect;
        private bool _muted;
        private bool _inFlight;
        private float _startedAt;
        private float _nextPing;
        private float _nextMicTry;
        private float _nextPaint;
        private string _activity = "";
        private string _herText = "";

        private Component _status;
        private Component _heard;
        private Component _her;
        private RectTransform _levelFill;
        private Button _muteButton;
        private GameObject _micOnIcon;
        private GameObject _micOffIcon;

        public static void Build(RectTransform body, NextShell shell)
        {
            Create(body, shell).Dial();
        }

        /// <summary>
        /// 她打來、主人已經接了（`NextIncomingCall` 的 accept 成功、伺服器轉 live）：不撥號，直接進通話。
        /// </summary>
        public static void BuildAnswered(RectTransform body, NextShell shell, string callId)
        {
            var view = Create(body, shell);
            if (_active != null && _active != view) _active.Finish("", true, false);
            _active = view;
            view._callId = callId ?? "";
            view._dialedAt = Time.realtimeSinceStartup;
            view.StartLive("accepted");
        }

        private static NextCallView Create(RectTransform body, NextShell shell)
        {
            var column = UiBuilder.Column("CallBody", body, 10f, new RectOffset(12, 12, 10, 16), true, TextAnchor.UpperCenter);
            UiBuilder.Weight(column, 1f);
            var view = column.gameObject.AddComponent<NextCallView>();
            view._shell = shell;
            view._entity = shell.EntityId;
            view.BuildUi(column);
            return view;
        }

        /// <summary>登出／token 失效時，殼趁 token 還在先掛掉這一通。</summary>
        /// <remarks>
        /// 也要**放下「等舊的那一通收乾淨」**（`_retiring`）：殼接著就 `Session.Close()`，
        /// 這一段登入還在路上的 hangup／撥號回包都會被 `NextApi` 丟掉 ⇒ `CompleteRetire` 永遠不會來，
        /// 重新登入後再撥就一直卡在「発信中…」（2026-10-29 review）。放下是安全的：伺服器的 hangup
        /// 帶 `call_id`，舊請求晚到也收不掉下一段登入撥的那一通；晚到的撥號回包也一樣被丟掉。
        /// </remarks>
        public static void HangUpActive()
        {
            if (_active != null) _active.Finish("", notifyServer: true, leave: false);
            if (!ReferenceEquals(_retiring, null))
            {
                _retiring._afterRetire = null;
                _retiring = null;
            }
        }

        // ── 畫面 ────────────────────────────────────────────────────────────────

        private void BuildUi(RectTransform column)
        {
            NextUi.Header("CallHead", column, NextStrings.Get("call_title"), () => Finish("", true, true));

            StageAvatar stage = null;
            try { stage = NextStageView.Build(column, _entity, AvatarTimeline.Normal, NextConfig.BackgroundFor(_entity)); }
            catch (Exception error) { Debug.LogWarning("[call] 立繪載入失敗：" + error.Message); }
            _shell.Stage = stage;

            var name = UiBuilder.Text("CallName", column, _shell.EntityDisplayName, NextTheme.HeadSize, NextTheme.Ink,
                                      TextAlignmentOptions.Center, noWrap: true, bold: true);
            NextUi.Italic(name);
            _status = UiBuilder.Text("CallStatus", column, NextStrings.Get("call_dialing"), NextTheme.LabelSize,
                                     NextTheme.SkyDeep, TextAlignmentOptions.Center, noWrap: true, bold: true);

            // 音量條：淡藍槽＋天藍填色（寬度跟著電平；靜音時歸零）
            var track = UiBuilder.Row("CallLevel", column, 0f, null, TextAnchor.MiddleLeft);
            UiBuilder.FixedHeight(track, 6f);
            var trackBg = UiBuilder.Backdrop("CallLevelBg", track, NextTheme.SkyPale, radius: 3f);
            trackBg.SetAsFirstSibling();
            _levelFill = UiBuilder.NewRect("CallLevelFill", track);
            _levelFill.gameObject.AddComponent<LayoutElement>().ignoreLayout = true;
            _levelFill.anchorMin = new Vector2(0f, 0f);
            _levelFill.anchorMax = new Vector2(0f, 1f);
            _levelFill.offsetMin = Vector2.zero;
            _levelFill.offsetMax = Vector2.zero;
            var fill = _levelFill.gameObject.AddComponent<Image>();
            UiBuilder.Round(fill, 3f);
            fill.color = NextTheme.Sky;
            fill.raycastTarget = false;

            // 最近一來一往：我說的（辨識結果）／她說的
            var card = NextUi.Card("CallLines", column);
            _heard = UiBuilder.Text("CallHeard", card, NextStrings.Get("call_hint"), NextTheme.BodySize, NextTheme.InkSoft,
                                    TextAlignmentOptions.TopLeft);
            NextUi.Divider("CallLinesDivider", card);
            _her = UiBuilder.Text("CallHer", card, "", NextTheme.BodySize, NextTheme.Ink, TextAlignmentOptions.TopLeft);
            NextFonts.UseHer(_her);

            var spacer = UiBuilder.NewRect("CallSpacer", column);
            UiBuilder.Weight(spacer, 1f);

            var controls = UiBuilder.Row("CallControls", column, 36f, null, TextAnchor.MiddleCenter);
            UiBuilder.FixedHeight(controls, 72f);
            _muteButton = NextUi.IconButton("CallMute", controls, "mic", NextUi.Style.Secondary, 56f, round: true);
            _muteButton.onClick.AddListener(ToggleMute);
            // 靜音鈕兩顆圖示輪流顯示（停用的那顆不參與排版）
            var onIcon = _muteButton.transform.Find("CallMuteIcon");
            _micOnIcon = onIcon != null ? onIcon.gameObject : null;
            _micOffIcon = UiBuilder.Icon("CallMuteOffIcon", "mic-off", _muteButton.transform, Mathf.Round(56f * 0.52f),
                                         NextTheme.Danger).gameObject;
            _micOffIcon.SetActive(false);
            var hangup = NextUi.IconButton("CallHangup", controls, "phone-off", NextUi.Style.Danger, 68f, round: true);
            hangup.onClick.AddListener(() => Finish("", true, true));
        }

        private void Paint()
        {
            if (_status == null) return;
            string line;
            if (_phase == Phase.Dialing) line = NextStrings.Get("call_dialing");
            else if (_phase == Phase.Declined) line = _declinedText;
            else if (_phase == Phase.Ended) line = NextStrings.Get("call_ended");
            else
            {
                var seconds = Mathf.Max(0, Mathf.FloorToInt(Time.realtimeSinceStartup - _startedAt));
                var clock = (seconds / 60).ToString("00") + ":" + (seconds % 60).ToString("00");
                var doing = _muted ? NextStrings.Get("call_muted") : _activity;
                line = string.IsNullOrEmpty(doing) ? clock : clock + " · " + doing;
            }
            NextUi.SetText(_status, line);
            if (_levelFill != null)
            {
                var level = _muted || _phase != Phase.Live ? 0f : Mathf.Clamp01((_levelDb + 60f) / 45f);
                _levelFill.anchorMax = new Vector2(level, 1f);
            }
        }

        private void SetActivity(string key)
        {
            _activity = string.IsNullOrEmpty(key) ? "" : NextStrings.Get(key);
            Paint();
        }

        private void ToggleMute()
        {
            _muted = !_muted;
            _pending.Clear();
            _frames.Clear();
            if (_muted && _phase == Phase.Live) _shell.Api.Call("interrupt", _entity, _callId, _ => { });
            if (_micOnIcon != null) _micOnIcon.SetActive(!_muted);
            if (_micOffIcon != null) _micOffIcon.SetActive(_muted);
            Paint();
        }

        // ── 撥／收線 ─────────────────────────────────────────────────────────────

        private void Dial()
        {
            if (this == null || _phase == Phase.Ended) return;
            var previous = _active != null && _active != this ? _active : _retiring;
            if (!ReferenceEquals(previous, null) && previous != this)
            {
                // 等舊撥號＋hangup 都完成再撥：兩個 POST 不保證到達順序，舊請求不能收掉新 session。
                previous.Finish("", true, false, Dial);
                return;
            }
            _active = this;
            _phase = Phase.Dialing;
            _dialedAt = Time.realtimeSinceStartup;
            Paint();
            var api = _shell.Api;
            var entity = _entity;
            _dialRequestInFlight = true;
            _shell.Api.Call("call", entity, "", reply =>
            {
                _dialRequestInFlight = false;
                if (this == null || _phase == Phase.Ended)
                {
                    // 撥號途中就離開了：只掛伺服器實際接通的那一通，再允許下一頁重撥。
                    if (reply.Ok)
                    {
                        var lateId = reply.Json.OptJObject("session")?.OptString("id") ?? "";
                        api.Call("hangup", entity, lateId, _ => CompleteRetire());
                    }
                    else CompleteRetire();
                    return;
                }
                if (!reply.Ok && reply.Code == "call_declined" && reply.Json != null)
                {
                    StartCoroutine(Declined(reply.Json.OptString("reason"), reply.Json.OptString("line")));
                    return;
                }
                if (!reply.Ok)
                {
                    var busy = reply.Code == "busy_elsewhere";
                    Finish(busy ? NextStrings.Get("call_busy_elsewhere")
                                : NextStrings.F("call_failed", "reason", reply.UserMessage()), false, true);
                    return;
                }
                _callId = reply.Json.OptJObject("session")?.OptString("id") ?? "";
                if (reply.Json.OptString("state") == "ringing" && !string.IsNullOrEmpty(_callId))
                {
                    Debug.Log("[call] 響鈴中：" + _callId);
                    StartCoroutine(WaitForPickup());
                    return;
                }
                StartLive("");
            });
        }

        /// <summary>她接了嗎：響鈴期間輪詢 `/v1/calls/&lt;id&gt;/state`（兩階段響鈴，2026-11-17）。</summary>
        private IEnumerator WaitForPickup()
        {
            var until = Time.realtimeSinceStartup + RingMaxSeconds;
            while (Time.realtimeSinceStartup < until)
            {
                if (this == null || _phase != Phase.Dialing) yield break;
                NextApi.Reply got = null;
                var done = false;
                _shell.Api.Call("state", _entity, _callId, r => { got = r; done = true; });
                while (!done) yield return null;
                if (this == null || _phase != Phase.Dialing) yield break;
                var state = got != null && got.Ok ? got.Json.OptString("state") : "";
                if (state == "live")
                {
                    // 她 0 ms 就接也要看得見響鈴：至少響 RingMinSeconds 再進通話
                    while (Time.realtimeSinceStartup - _dialedAt < RingMinSeconds) yield return null;
                    StartLive(got.Json.OptString("pickup"));
                    yield break;
                }
                if (state == "declined")
                {
                    StartCoroutine(Declined(got.Json.OptString("reason"), got.Json.OptString("line")));
                    yield break;
                }
                if (state == "cancelled" || state == "failed" || state == "ended" || state == "")
                {
                    Debug.Log("[call] 響鈴沒有結果：" + state + " code=" + (got != null ? got.Code : ""));
                    Finish(NextStrings.Get("call_failed"), false, true);
                    yield break;
                }
                yield return new WaitForSeconds(RingPollSeconds);
            }
            // 20 秒還沒有結論：當沒接（不要留一通沒有聲音的通話）
            StartCoroutine(Declined("asleep", ""));
        }

        /// <summary>真的接通：計時、心跳、事件流、麥克風，順便問一次語音引擎健康。</summary>
        private void StartLive(string pickup)
        {
            _phase = Phase.Live;
            _startedAt = Time.realtimeSinceStartup;
            _nextPing = _startedAt + PingSeconds;
            _eventCursor = 0;
            _epoch = 0;
            Debug.Log("[call] 接通：" + _callId + " pickup=" + pickup);
            OpenCallEvents();
            StartMic();
            Paint();
            CheckVoiceEngine();
        }

        /// <summary>語音引擎不通時明說（否則用戶只會覺得她不理人）。</summary>
        private void CheckVoiceEngine()
        {
            if (string.IsNullOrEmpty(_callId)) return;
            _shell.Api.Get("/v1/voice/status", reply =>
            {
                if (this == null || _phase != Phase.Live || !reply.Ok) return;
                if (reply.Json.OptBoolean("ok")) return;
                Debug.Log("[call] 語音引擎不通：" + reply.Body);
                NextUi.SetText(_heard, NextStrings.Get("call_voice_down"));
            });
        }

        /// <summary>她沒接：照理由響一陣、換成拒接畫面、停一下再回聊天頁（中途按掛斷就直接走）。</summary>
        private IEnumerator Declined(string reason, string line)
        {
            Debug.Log("[call] 她沒接：" + reason);
            var ring = reason == "asleep" ? NoAnswerRingSeconds : reason == "on_call" ? 0f : DeclineRingSeconds;
            while (Time.realtimeSinceStartup - _dialedAt < ring)
            {
                if (this == null || _phase != Phase.Dialing) yield break;
                yield return null;
            }
            if (this == null || _phase != Phase.Dialing) yield break;
            if (_active == this) _active = null;      // 沒有伺服器那一通，不必等收線就能重撥
            if (reason == "asleep") _declinedText = NextStrings.Get("call_no_answer");
            else if (reason == "on_call") _declinedText = NextStrings.Get("call_line_busy");
            else _declinedText = NextStrings.Get("call_declined");
            _phase = Phase.Declined;
            var spoke = !string.IsNullOrEmpty(line);
            if (spoke)
            {
                ShowHer(line);
                NextUi.SetText(_heard, NextStrings.Get("call_declined_hint"));
            }
            else NextUi.SetText(_heard, "");
            Paint();
            var until = Time.realtimeSinceStartup + (spoke ? DeclinedLineShowSeconds : DeclinedShowSeconds);
            while (Time.realtimeSinceStartup < until)
            {
                if (this == null || _phase != Phase.Declined) yield break;
                yield return null;
            }
            if (this == null || _phase != Phase.Declined) yield break;
            Finish(_declinedText, false, true);
        }

        private void StartMic()
        {
#if BA_SELFTEST
            if (NoMic) { SetActivity("call_listening"); return; }
#endif
            if (_mic == null)
            {
                _mic = gameObject.AddComponent<MicCapture>();
                // 通話音源：Android 的 VOICE_COMMUNICATION 才掛得上平台回音消除（她的聲音別進麥克風）
                _mic.Source = "voice_communication";
                _mic.Effects = true;
                _mic.Samples = OnSamples;
            }
            if (_mic.Running) return;
            if (_mic.Start())
            {
                Debug.Log("[call] 收音：" + _mic.Info + "／AEC=" + _mic.EchoCancelled);
                SetActivity(_mic.EchoCancelled ? "call_listening" : "call_listening_no_aec");
                return;
            }
            // 第一次會先跳權限框（回 false）；之後每秒再試，直到開成
            SetActivity("call_mic_wait");
            _nextMicTry = Time.realtimeSinceStartup + 1f;
        }

        /// <summary>
        /// 收線（唯一的出口）。<paramref name="message"/> 非空 ⇒ 回聊天頁時留一句提示；
        /// <paramref name="notifyServer"/>：要不要叫伺服器掛（伺服器已經說沒了就不用）；
        /// <paramref name="leave"/>：要不要回聊天頁（物件銷毀途中／登出時不用）。
        /// <paramref name="done"/>：重撥等接續動作；等舊撥號與 hangup 都收完才叫。
        /// </summary>
        private void Finish(string message, bool notifyServer, bool leave, Action done = null)
        {
            if (_phase == Phase.Ended)
            {
                if (done != null)
                {
                    if (ReferenceEquals(_retiring, this)) _afterRetire += done;
                    else done();
                }
                return;
            }
            var wasLive = !string.IsNullOrEmpty(_callId);
            // 取證：把「誰收掉這通」寫進裝置日誌（2026-11-08 通話自斷，只有訊息沒有來源）。
            var caller = new System.Diagnostics.StackTrace().GetFrame(1);
            Debug.Log("[call] 收線來源：Finish(" + (message ?? "") + ") ← " +
                      (caller == null || caller.GetMethod() == null ? "?" : caller.GetMethod().Name));
            var waiting = _dialRequestInFlight;
            _phase = Phase.Ended;
            if (_active == this) _active = null;
            _pending.Clear();
            _frames.Clear();
            if (_callEvents != null) StopCoroutine(_callEvents);
            _callEvents = null;
            if (_shell != null) _shell.Api.CancelCallEvents();
            if (_mic != null && _mic.Running) _mic.Stop();
            if (_shell != null)
            {
                _shell.StopSpeech();
                if (notifyServer && wasLive)
                {
                    waiting = true;
                    _shell.Api.Call("hangup", _entity, _callId, _ => CompleteRetire());
                }
            }
            _inFlight = false;
            Debug.Log("[call] 收線：" + (string.IsNullOrEmpty(message) ? "-" : message));
            if (waiting)
            {
                _retiring = this;
                if (done != null) _afterRetire += done;
            }
            else done?.Invoke();
            if (!leave || _shell == null) return;
            _shell.CloseCall();
            if (!string.IsNullOrEmpty(message) && _shell.Chat != null) _shell.Chat.Notice(message);
        }

        private void CompleteRetire()
        {
            if (ReferenceEquals(_retiring, this)) _retiring = null;
            var done = _afterRetire;
            _afterRetire = null;
            done?.Invoke();
        }

        private void OnDestroy()
        {
            Finish("", notifyServer: true, leave: false);
        }

        private void OnApplicationPause(bool paused)
        {
            // 2026-11-08：以前一進背景就整通掛斷（裝置日誌實證「通話が切れました」22 秒後自斷）。
            // 改成「掛起」：麥克風停、畫面留著；回前景若這通還活著就繼續（服務端 45 秒沒心跳才收）。
            if (paused)
            {
                Debug.Log("[call] 進背景：通話掛起（不收線）");
                if (_mic != null && _mic.Running) _mic.Stop();
                return;
            }
            if (_phase != Phase.Live) return;
            Debug.Log("[call] 回前景：續這通");
            _nextEventsReconnect = Time.realtimeSinceStartup;
            _nextMicTry = Time.realtimeSinceStartup;
            if (_mic != null && _mic.Running) StartMic();
        }

        // ── 每幀 ────────────────────────────────────────────────────────────────

        private void Update()
        {
            if (_phase != Phase.Live || _shell == null) return;
            var now = Time.realtimeSinceStartup;
            if (_mic != null && !_mic.Running && now >= _nextMicTry) StartMic();
            if (_callEvents == null && now >= _nextEventsReconnect) OpenCallEvents();
            if (now >= _nextPing)
            {
                _nextPing = now + PingSeconds;
                var id = _callId;
                _shell.Api.Call("ping", _entity, id, reply =>
                {
                    if (this == null || _phase != Phase.Live || id != _callId) return;
                    if (reply.Status == 410 || reply.Status == 404)
                        Finish(NextStrings.Get("call_dropped"), false, true);
                });
            }
            if (now >= _nextPaint)
            {
                _nextPaint = now + 0.1f;
                if (!_inFlight && !_shell.IsSpeaking && !_muted && _mic != null && _mic.Running
                    && _activity != NextStrings.Get("call_listening") && _activity != NextStrings.Get("call_listening_no_aec"))
                {
                    SetActivity(_mic.EchoCancelled ? "call_listening" : "call_listening_no_aec");
                    _shell.SetStage(AvatarTimeline.Normal);
                }
                Paint();
            }
        }

        // ── 收音 → 100 ms 串行小帧；切句只在 App Server ─────────────────────────

        private void OnSamples(short[] pcm, int count)
        {
            if (_phase != Phase.Live || _muted || pcm == null) return;
            count = Math.Min(count, pcm.Length);
            double energy = 0;
            for (var i = 0; i < count; i++)
            {
                energy += (double)pcm[i] * pcm[i];
                _pending.Add(pcm[i]);
                if (_pending.Count < FrameSamples) continue;
                var samples = _pending.ToArray();
                var bytes = new byte[FrameSamples * 2];
                Buffer.BlockCopy(samples, 0, bytes, 0, bytes.Length);
                _frames.Enqueue(bytes);
                _pending.Clear();
            }
            var rms = Math.Sqrt(energy / Math.Max(1, count));
            _levelDb = rms < 1 ? -90f : (float)(20.0 * Math.Log10(rms / 32768.0));
            if (_frames.Count > MaxQueuedFrames)
            {
                // 以前是「積壓 > 50 就整通掛斷」——實際事故：慢回包 450 ms/幀 ⇒ 6.5 秒掛斷。
                // 現在只丟最舊的那些（丟幾百毫秒音訊，換通話活著），並留一行日誌。
                var dropped = 0;
                while (_frames.Count > MaxQueuedFrames - 50 && _frames.Count > 0)
                {
                    _frames.Dequeue();
                    dropped++;
                }
                Debug.LogWarning("[call] 上行積壓，丟舊幀 " + dropped + " 個（佇列 " + _frames.Count + "）");
            }
            SendNextFrame();
        }

        private void SendNextFrame()
        {
            if (_frameSending || _frames.Count == 0 || _phase != Phase.Live) return;
            _frameSending = true;
            var id = _callId;
            // 「像打電話」Phase 1：一次最多帶 FramesPerRequest 幀（請求數 10/s → 2.5/s）。
            var batch = new List<byte[]>(FramesPerRequest);
            while (batch.Count < FramesPerRequest && _frames.Count > 0) batch.Add(_frames.Dequeue());
            var seq = _nextFrameSeq;
            _nextFrameSeq += batch.Count;
            _shell.Api.SendCallFrames(id, batch, seq, reply =>
            {
                _frameSending = false;
                if (this == null || _phase != Phase.Live || id != _callId) return;
                if (!reply.Ok)
                {
                    // 只有「服务端明确说这通没了」才收线；其它（超时／5xx／409 乱序）丢掉这批继续，
                    // 因为丢几百毫秒音频远好过整通断掉。
                    Debug.Log("[call] 音幀沒送成：batch=" + batch.Count + " " + reply.Describe());
                    if (reply.Status == 410 || reply.Status == 404)
                    {
                        Finish(NextStrings.Get("call_dropped"), false, true);
                        return;
                    }
                    if (reply.Status == 409 && reply.Json != null)
                    {
                        // 409 = seq 乱序：把游标对回服务端期望值，否则之后每一批都会被拒（整通没声音）。
                        var expected = reply.Json.OptInt("expected");
                        if (expected > 0 && expected != _nextFrameSeq)
                        {
                            Debug.Log("[call] seq 重對：本機 " + _nextFrameSeq + " → 服務端 " + expected);
                            _nextFrameSeq = expected;
                        }
                    }
                }
                SendNextFrame();
            });
        }

        private void OpenCallEvents()
        {
            if (_phase != Phase.Live || _callEvents != null || string.IsNullOrEmpty(_callId)) return;
            var id = _callId;
            _callEvents = _shell.Api.CallEvents(id, _eventCursor, OnCallFrame, failure =>
            {
                _callEvents = null;
                if (this == null || _phase != Phase.Live || id != _callId) return;
                if (failure.Contains(" 409") || failure.Contains(" 410") || failure.Contains(" 404"))
                {
                    // 2026-11-08：這裡以前直接收線，實際通話因此「說了兩句就斷」。
                    // 事件流斷掉是**可恢復**的：409（游標過期）把游標歸零重放，其它就等 1 秒重連；
                    // 只有 404（這通真的不存在）才收線。
                    if (failure.Contains(" 404") && !failure.Contains(" 409") && !failure.Contains(" 410"))
                    {
                        Finish(NextStrings.Get("call_dropped"), false, true);
                        return;
                    }
                    Debug.Log("[call] 事件線重連（可恢復）：" + failure);
                    if (failure.Contains(" 409")) _eventCursor = 0;
                    _nextEventsReconnect = Time.realtimeSinceStartup + 1f;
                    return;
                }
                if (!string.IsNullOrEmpty(failure)) Debug.Log("[call] 事件線重連：" + failure);
                _nextEventsReconnect = Time.realtimeSinceStartup + 1f;
            });
        }

        private void OnCallFrame(string name, JObject data)
        {
            if (this == null || _phase != Phase.Live) return;
            var seq = data.OptInt("_seq");
            if (seq > 0)
            {
                if (seq <= _eventCursor) return;
                _eventCursor = seq;
            }
            var eventEpoch = data.OptInt("epoch", _epoch);
            if (eventEpoch < _epoch) return;       // 舊世代的聲音／文字不補播
            _epoch = eventEpoch;
            var payload = data.OptJObject("payload");
            switch (name)
            {
                case "interrupt":
                    _shell.StopSpeech();
                    _inFlight = false;
                    _shell.SetStage(AvatarTimeline.Normal);
                    SetActivity("call_hearing");
                    Debug.Log("[call] 服務端切句偵測到開口：打斷 epoch=" + _epoch);
                    break;
                case "utterance":
                    _inFlight = true;
                    _herText = "";
                    SetActivity("call_thinking");
                    _shell.SetStage(AvatarTimeline.Thinking);
                    break;
                case "transcript":
                    var heard = payload != null ? payload.OptString("text") : data.OptString("text");
                    NextUi.SetText(_heard, NextStrings.F("call_you_said", "text", heard));
                    break;
                case "delta":
                    if (payload != null) ShowHer(_herText + payload.OptString("text"));
                    break;
                case "speech":
                    if (payload != null) _shell.PlaySpeech(payload.OptString("audio"));
                    SetActivity("call_speaking");
                    _shell.SetStage(AvatarTimeline.Speaking);
                    break;
                case "done":
                    _inFlight = false;
                    if (payload != null && !string.IsNullOrEmpty(payload.OptString("text")))
                        ShowHer(payload.OptString("text"));
                    break;
                case "call":
                    if (payload != null && payload.OptString("state") == "ended")
                        Finish(NextStrings.Get("call_her_hangup"), false, true);
                    break;
                case "error":
                    _inFlight = false;
                    SetActivity(data.OptString("reason") == "stt_failed" ? "call_not_heard" : "call_error");
                    _shell.SetStage(AvatarTimeline.Normal);
                    break;
                case "activity":
                    if (!_inFlight && !_shell.IsSpeaking) SetActivity("call_listening");
                    break;
            }
        }

        private void ShowHer(string text)
        {
            _herText = text ?? "";
            NextUi.SetText(_her, _herText);
        }
    }
}
