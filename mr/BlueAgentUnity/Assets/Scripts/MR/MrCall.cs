using System;
using BlueAgent.Logic;
using BlueAgent.Next;
using BlueAgent.Spike;
using UnityEngine;
#if UNITY_ANDROID && !UNITY_EDITOR
using UnityEngine.Android;
#endif

namespace BlueAgent.MR
{
    /// <summary>
    /// MR 端的**通話模式**（owner 2026-11-01「接近 app 的通話模式」）：
    /// 右手雙擊撥／掛；接通後一直聽 → `CallVad` 斷句 → 自動送 → 她出聲；你一開口就打斷她；15 秒心跳。
    ///
    /// 邏輯照手機的 `NextCallView` 抄、剝掉 UI：畫面只剩頭頂氣泡（`MrBridge` 的字）＋儀表一行（<see cref="Line"/>）。
    /// 服務端那條單人通話是現成的（`POST /api/voice/*`＋`/api/audio` 的 `X-Call`），這裡一行協議都不加。
    ///
    /// 收音用手機那支 `MicCapture`（原生 `AudioRecord`＋`voice_communication` 音源＋平台 AEC；
    /// `MicRecord.java`／`MicFx.java` 的副本在 MR 的 androidlib 裡）。AEC 掛不上 ⇒ 她說話時要比她大聲才打得斷
    /// （`CallVad.Echo`），儀表明示「無回音消除」，不假裝掛上了。
    ///
    /// 一次只有一句在飛：你開口（`Started`）時若還有一句在飛、或她在說，就先打斷那一輪（票號自增，晚到的幀全丟），
    /// 所以新的一句送出去時前一句一定已經不在了。**不走** `MrBridge.Busy`（那只是狀態）。
    /// </summary>
    public sealed class MrCall : MonoBehaviour
    {
        /// <summary>心跳間隔（秒）；伺服器 45 秒沒聽到就自己掛（之後的心跳／句子回 410）。</summary>
        private const float PingSeconds = 15f;
        /// <summary>「沒聽清楚」「沒送到」這類一次性的提示留多久（秒）。</summary>
        private const float NoticeSeconds = 3f;
        /// <summary>
        /// 通話中每隔多久印一行電平（診斷；2026-11-01 實機：Quest 沒有 AGC，三通裡兩通她完全沒回，
        /// 分不出是「沒聽到你」還是「送了沒回」）。
        /// </summary>
        private const float LevelLogSeconds = 3f;
        /// <summary>
        /// 收音的軟體增益（倍）。Quest 3 的 `voice_communication` 沒有 AGC、電平很低（2026-11-08 實機：正常講話峰值
        /// 約 -30 dB、字與字之間被壓到 -90，一句話湊不滿 VAD 的 160 ms 起音／300 ms 有聲 ⇒ 整句被丟、她不回）。
        /// 噪音底乾淨（約 -80 dB），放大 ×4（+12 dB）再進 VAD 與 STT；手機那條路不動。
        /// </summary>
        private const float InputGain = 4f;
        private short[] _gained = new short[0];

        public enum Phase { Idle, Dialing, Live }

        public Phase State { get; private set; } = Phase.Idle;
        /// <summary>撥號中或通話中（右手雙擊＝掛）。</summary>
        public bool Active => State != Phase.Idle;
        /// <summary>該讓你知道的通話事件（撥不通、怎麼結束的、沒聽清楚…；已經是這一種語言的字）⇒ 視野下方的通知。</summary>
        public event Action<string> Notice;

        /// <summary>麥克風權限被拒（儀表要明示，不然通了也沒反應）。</summary>
        public bool MicDenied => _permissionDenied || (_mic != null && _mic.Denied);

        private MrBridge _bridge;
        private MicCapture _mic;
        private readonly CallVad _vad = new CallVad();
        private string _entity = "";
        private string _callId = "";
        /// <summary>每撥一次、每送一句、每打斷一次就 +1：回來的東西票號不對就丟。</summary>
        private int _ticket;
        private bool _inFlight;
        private float _nextPing;
        private float _nextMicTry;
        private bool _permissionDenied;
        /// <summary>收線後留在儀表上的那一句（「已收線」「她在 Discord 語音房」…）；下次撥號清掉。</summary>
        private string _ended = "";
        private string _notice = "";
        private float _noticeUntil;
        private float _nextLevelLog;
        /// <summary>上次印電平之後最大的那一格（dBFS）。</summary>
        private float _peakDb = -90f;
        private float _speechStartedAt;
        private float _sentAt;

        public static MrCall Create(MrBridge bridge)
        {
            var call = new GameObject("MR Call").AddComponent<MrCall>();
            call._bridge = bridge;
            return call;
        }

        /// <summary>啟動時先要麥克風權限（第一次撥號時才跳對話框會打斷手勢）。</summary>
        public void AskPermission()
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            if (Permission.HasUserAuthorizedPermission(Permission.Microphone)) return;
            var callbacks = new PermissionCallbacks();
            callbacks.PermissionDenied += _ => _permissionDenied = true;
            callbacks.PermissionGranted += _ => _permissionDenied = false;
            Permission.RequestUserPermission(Permission.Microphone, callbacks);
#endif
        }

        /// <summary>儀表上通話那一行（沒在通、也沒有收線提示 ⇒ 空）。</summary>
        public string Line
        {
            get
            {
                if (State == Phase.Idle) return _ended;
                if (State == Phase.Dialing) return NextStrings.Get("call_dialing");
                if (Time.unscaledTime < _noticeUntil) return _notice;
                if (_mic == null || !_mic.Running) return NextStrings.Get(MicDenied ? "mr_mic_denied" : "call_mic_wait");
                if (_vad.Speaking) return NextStrings.Get("call_hearing");
                if (_inFlight && _bridge.Thinking) return NextStrings.Get("call_thinking");
                if (_bridge.Speaking) return NextStrings.Get("call_speaking");
                return NextStrings.Get(_mic.EchoCancelled ? "call_listening" : "mr_call_no_aec");
            }
        }

        /// <summary>右手雙擊：沒在通 ⇒ 撥；在通（或撥號中）⇒ 掛。</summary>
        public void Toggle()
        {
            if (Active) Hangup(NextStrings.Get("call_ended"));
            else Dial();
        }

        private void Dial()
        {
            if (!_bridge.Ready)
            {
                _ended = NextStrings.Get("mr_call_not_ready");
                Notice?.Invoke(_ended);
                Debug.Log("[mr] 通話：橋還沒連上 ⇒ 不撥");
                return;
            }
            if (!_bridge.Master)
            {
                // `X-Call` 只認 master（APP-API-SSE §通話）：guest 撥了也是 403 ⇒ 不撥、明示
                _ended = NextStrings.Get("mr_call_guest");
                Notice?.Invoke(_ended);
                Debug.Log("[mr] 通話：guest token ⇒ 不撥（伺服器只讓 master 通話，會 403）");
                return;
            }
            State = Phase.Dialing;
            _ended = "";
            _entity = _bridge.Entity;
            var entity = _entity;
            var ticket = ++_ticket;
            Debug.Log($"[mr] 通話：撥給 {entity}");
            _bridge.Api.Call("call", entity, "", reply =>
            {
                if (this == null) return;
                if (ticket != _ticket || State != Phase.Dialing)
                {
                    // 撥號途中就掛了（或換了人）：只掛伺服器實際接通的那一通
                    var late = reply.Ok ? reply.Json.OptJObject("session")?.OptString("id") ?? "" : "";
                    if (!string.IsNullOrEmpty(late)) _bridge.Api.Call("hangup", entity, late, _ => { });
                    return;
                }
                if (!reply.Ok)
                {
                    State = Phase.Idle;
                    _ended = reply.Code == "busy_elsewhere" ? NextStrings.Get("call_busy_elsewhere")
                        : reply.Status == 403 ? NextStrings.Get("mr_call_guest")
                        : NextStrings.F("call_failed", "reason", reply.UserMessage());
                    Notice?.Invoke(_ended);
                    Debug.LogWarning($"[mr] 通話：撥不通（{reply.Describe()}）");
                    return;
                }
                // 同一位已經在通（例如手機那邊開著）⇒ `existing:true`，照樣用這一通
                _callId = reply.Json.OptJObject("session")?.OptString("id") ?? "";
                State = Phase.Live;
                _nextPing = Time.unscaledTime + PingSeconds;
                _vad.Reset();
                _nextMicTry = 0f;                               // 這一通開不了麥要再說一次
                _nextLevelLog = Time.unscaledTime + LevelLogSeconds;
                _peakDb = -90f;
                Debug.Log($"[mr] 通話：接通 {_callId}{(reply.Json.OptBoolean("existing") ? "（沿用已在通的那一通）" : "")}");
                StartMic();
            });
        }

        /// <summary>
        /// 收線（唯一的出口）。<paramref name="message"/>＝之後留在儀表上的那一句；
        /// <paramref name="notifyServer"/>：伺服器已經說沒了（410、她自己掛）就不必再叫它掛。
        /// </summary>
        public void Hangup(string message, bool notifyServer = true)
        {
            if (State == Phase.Idle) return;
            var id = _callId;
            var entity = _entity;
            State = Phase.Idle;
            _ticket++;
            _callId = "";
            _ended = message ?? "";
            if (!string.IsNullOrEmpty(_ended)) Notice?.Invoke(_ended);
            if (_mic != null && _mic.Running) _mic.Stop();
            _vad.Reset();
            if (_bridge != null)
            {
                _bridge.StopSpeech();
                if (_inFlight) _bridge.Api.CancelTurn();
                _bridge.EndTurn();
                if (notifyServer && !string.IsNullOrEmpty(id))
                    _bridge.Api.Call("hangup", entity, id,
                                     reply => Debug.Log($"[mr] 通話：hangup {(reply.Ok ? "200" : reply.Describe())}"));
            }
            _inFlight = false;
            Debug.Log($"[mr] 通話：收線（{(string.IsNullOrEmpty(message) ? "-" : message)}）");
        }

        private void StartMic()
        {
            if (_mic == null)
            {
                _mic = gameObject.AddComponent<MicCapture>();
                // VOICE_COMMUNICATION 才掛得上平台回音消除（她從喇叭出來的聲音別進麥）；手機通話同一組設定
                _mic.Source = "voice_communication";
                _mic.Effects = true;
                _mic.Samples = OnSamples;
            }
            if (_mic.Running) return;
            if (_mic.Start())
            {
                // 驗收 1：grep `[mr] 收音`
                Debug.Log($"[mr] 收音：{_mic.Info} aec={(_mic.EchoCancelled ? "true" : "false")} 增益×{InputGain:F0}");
                return;
            }
            // 第一次會先跳權限框（回 false）；之後每秒再試，直到開成
            if (_nextMicTry <= 0f || MicDenied) Notice?.Invoke(NextStrings.Get(MicDenied ? "mr_mic_denied" : "call_mic_wait"));
            _nextMicTry = Time.unscaledTime + 1f;
        }

        private void Update()
        {
            if (State != Phase.Live || _bridge == null) return;
            var now = Time.unscaledTime;
            if (_mic != null && !_mic.Running && now >= _nextMicTry) StartMic();
            // 她在想／在說、平台又沒掛上回音消除 ⇒ 要比她大聲才打得斷（喇叭漏進來的她自己不算）。AEC 掛上了這條自動關
            _vad.Echo = (_bridge.Thinking || _bridge.Speaking) && (_mic == null || !_mic.EchoCancelled);
            if (now >= _nextLevelLog && _mic != null && _mic.Running)
            {
                _nextLevelLog = now + LevelLogSeconds;
                Debug.Log($"[mr] 通話：電平 最大 {_peakDb:F0} dB／噪音底 {_vad.NoiseDb:F0}／門檻 {_vad.Threshold:F0}"
                          + $"（echo={(_vad.Echo ? "抬高" : "否")}、她{(_bridge.Speaking ? "在說" : _bridge.Thinking ? "在想" : "安靜")}）");
                _peakDb = -90f;
            }
            if (now < _nextPing) return;
            _nextPing = now + PingSeconds;
            var id = _callId;
            _bridge.Api.Call("ping", _entity, id, reply =>
            {
                if (this == null || State != Phase.Live || id != _callId) return;
                // 410：被另一端接管（後撥的蓋掉先撥的）⇒ 退出並說「被其他終端接管」（不點名是哪一台，WO §7 W3）
                if (reply.Status == 410)
                    Hangup(NextStrings.Get(NextText.TakenOver(reply.Code) ? "call_taken_over" : "call_dropped"), false);
            });
        }

        // ── 收音 → 斷句 ─────────────────────────────────────────────────────────

        private void OnSamples(short[] pcm, int count)
        {
            if (State != Phase.Live) return;
            pcm = Amplify(pcm, count);                       // 另一份；MicCapture 的緩衝不改
            foreach (var item in _vad.Feed(pcm, count))
            {
                switch (item.Kind)
                {
                    case CallVad.Kind.Started:
                        _speechStartedAt = Time.unscaledTime;
                        Debug.Log($"[mr] 通話：聽到開口（電平 {_vad.LevelDb:F0} dB ≥ 門檻 {_vad.Threshold:F0}）");
                        if (_bridge.Speaking || _inFlight) BargeIn();
                        break;
                    case CallVad.Kind.Utterance:
                        Debug.Log($"[mr] 通話：一句說完（{(item.Samples != null ? item.Samples.Length : 0) / (float)CallVad.SampleRate:F1} s）⇒ 送");
                        Send(item.Samples);
                        break;
                    case CallVad.Kind.Dropped:
                        Debug.Log($"[mr] 通話：太短不算一句（開口後 {Time.unscaledTime - _speechStartedAt:F1} s）");
                        break;
                }
            }
            if (_vad.LevelDb > _peakDb) _peakDb = _vad.LevelDb;
        }

        /// <summary>×<see cref="InputGain"/>、超出就削頂（不繞回）。回自己的緩衝，不動呼叫端那一份。</summary>
        private short[] Amplify(short[] pcm, int count)
        {
            count = Math.Min(count, pcm.Length);
            if (_gained.Length < count) _gained = new short[count];
            for (var i = 0; i < count; i++)
            {
                var value = pcm[i] * InputGain;
                _gained[i] = (short)(value > short.MaxValue ? short.MaxValue : value < short.MinValue ? short.MinValue : value);
            }
            return _gained;
        }

        /// <summary>你開口打斷她：嘴先閉上（本機停播）、伺服器不再合成、上一輪回來的東西都不要了。</summary>
        private void BargeIn()
        {
            _ticket++;
            _bridge.StopSpeech();
            if (_inFlight) _bridge.Api.CancelTurn();
            _inFlight = false;
            _bridge.EndTurn();
            _bridge.Api.Call("interrupt", _entity, _callId, _ => { });
            Debug.Log("[mr] 通話：打斷");
        }

        private void Send(short[] samples)
        {
            if (samples == null || samples.Length == 0) return;
            if (_inFlight)
            {
                // `Started` 已經把在飛的那一句打斷了；走到這裡＝同一幀裡又冒出一句 ⇒ 丟（一次只一句在飛）
                Debug.Log("[mr] 通話：上一句還在飛 ⇒ 這一句丟掉");
                return;
            }
            var ticket = ++_ticket;
            _inFlight = true;
            _sentAt = Time.unscaledTime;
            _bridge.StopSpeech();
            _bridge.BeginTurn();
            var wav = WavBytes.Encode(samples, CallVad.SampleRate);
            // `NextConfig.Language` 是語音辨識的 `X-Lang`；通話一定要出聲
            _bridge.Api.SendAudio(wav, _entity, NextConfig.Language, true,
                                  (name, data) => OnFrame(ticket, name, data),
                                  failure => OnClosed(ticket, failure), _callId);
        }

        private void OnFrame(int ticket, string name, JObject data)
        {
            if (this == null || ticket != _ticket || State != Phase.Live) return;
            _bridge.Present(name, data);
            if (name == "transcript")
                Debug.Log($"[mr] 通話：伺服器聽到「{_bridge.Heard}」（送出後 {Time.unscaledTime - _sentAt:F1} s）");
            else if (name == "speech")
                Debug.Log($"[mr] 通話：一段聲音到了（送出後 {Time.unscaledTime - _sentAt:F1} s）");
            else if (name == "done")
                Debug.Log($"[mr] 通話：這一輪完（送出後 {Time.unscaledTime - _sentAt:F1} s，{_bridge.Said.Length} 字）");
            else if (name == "error")
                Debug.LogWarning("[mr] 通話：這一輪出錯 " + NextText.StreamErrorDetail(data));
            if (name != "call") return;
            // 她自己掛了電話（`voice.hangup` 工具）
            var payload = data.OptJObject("payload");
            if (payload != null && payload.OptString("state") == "ended")
                Hangup(NextStrings.Get("call_her_hangup"), false);
        }

        private void OnClosed(int ticket, string failure)
        {
            if (this == null || ticket != _ticket) return;
            _inFlight = false;
            _bridge.EndTurn();
            if (string.IsNullOrEmpty(failure)) return;
            Debug.Log("[mr] 通話：這一句沒送成：" + failure);
            // 410＝伺服器那一通已經沒了（45 秒沒心跳、被別處掛掉、被另一端接管）⇒ 明示收線，下次雙擊重撥（WO R5／W3）
            if (NextText.CallGone(failure, out var takenOver))
            {
                Hangup(NextStrings.Get(takenOver ? "call_taken_over" : "call_dropped"), false);
                return;
            }
            // 422＝沒聽清楚（STT 轉不出字）：不是錯，請他再說一次
            _notice = NextStrings.Get(failure.Contains(" 422") ? "call_not_heard" : "call_error");
            _noticeUntil = Time.unscaledTime + NoticeSeconds;
            Notice?.Invoke(_notice);
        }

        private void OnApplicationPause(bool paused)
        {
            // 摘下頭顯／進系統選單：Unity 不跑 Update、麥克風也停 ⇒ 這通其實已經斷了，掛乾淨
            if (paused) Hangup(NextStrings.Get("call_ended"));
        }

        private void OnDestroy()
        {
            Hangup("");
        }
    }
}
