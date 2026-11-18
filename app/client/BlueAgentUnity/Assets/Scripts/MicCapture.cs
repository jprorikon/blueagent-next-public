using System;
using BlueAgent.Logic;
using UnityEngine;

namespace BlueAgent.Spike
{
    /// <summary>
    /// **收音**（集成層）：Android＝原生 `AudioRecord`（`MicRecord.java`；音源＝設定 `audio_source`，
    /// 平台 NS／AGC／AEC 照 `MicEffects.attach()` 掛）、編輯器＝Unity 的 `Microphone`
    /// → 16 kHz／單聲道 → 環形緩衝（最後 3 秒）。
    ///
    /// 為什麼 Android 不用 Unity 的 `Microphone`（2026-10-18）：它的音源固定、掛不上 `AcousticEchoCanceler`
    /// ⇒ 她自己從喇叭出來的聲音原樣進麥。原 app 擋回音的第一層就是「可選音源＋平台 AEC」（`MicEffects.kt`），
    /// 第二層（她講話時閥門關、講完的聾窗）在 `EarsLink`。
    ///
    /// 為什麼是 16 kHz：橋側那條上行的契約就是 **raw PCM16LE、16 kHz、單聲道**（見
    /// `EarsProtocol`）；在手機上先轉成橋吃得下的形狀，橋那一側就不必再重採樣。
    ///
    /// 為什麼要**環形緩衝**：喚醒那一刻，他其實已經講了半句（「む、ミラ、あのね」）——
    /// 環形緩衝就是「觸發前那幾秒」，由 <see cref="Recent"/> 取出、當**這一輪的第一塊**送上去
    /// （`seed=1`；Kotlin 那一版的 `mic.recentAudio()` 同一件事）。沒有它，每一輪都會少掉前半句。
    ///
    /// ⚠️ **只做收音與緩衝，不做判斷**：切句在橋側（silero-vad）、判人在橋側（聲紋閘）——
    /// 這一支連 RMS 都只是**算出來給節拍器看**（`EarsStream.Level`），不自己判「有沒有在講話」。
    ///
    /// ⚠️ Android 要 `RECORD_AUDIO` 權限：這個專案是程式建的 app（沒有手寫 Manifest），
    /// 權限要走 `UnityEngine.Android.Permission` 當場要（見 `EnsurePermission`）。
    /// </summary>
    public sealed class MicCapture : MonoBehaviour
    {
        /// <summary>上行取樣率（橋側契約；`EarsProtocol.SampleRateHz` 同一件事）。</summary>
        public const int SampleRate = 16000;

        /// <summary>環形緩衝幾秒（＝「觸發前」最多送多長；Kotlin 那一版是 3 s）。</summary>
        public const int RingSeconds = 3;

        /// <summary>Unity 那一圈的長度（秒）——只是「多久輪詢一次」的節奏，1 s 剛好。</summary>
        private const int ClipSeconds = 1;

        /// <summary>音源（`audio_source`：voice_recognition／voice_communication／mic／camcorder／unprocessed／default）。</summary>
        public string Source = "voice_recognition";

        /// <summary>要不要掛平台效果（`platform_effects`）。</summary>
        public bool Effects = true;

        /// <summary>AEC 真的掛上了（`MicListener.echoCancelled`；決定她講完要不要聾窗）。從 ROM 給的結果讀，不假設。</summary>
        public bool EchoCancelled { get; private set; }

        /// <summary>這條收音路的樣子（`onMicInfo`：音源／裝置有什麼／真的掛上什麼）——診斷用。</summary>
        public string Info { get; private set; } = "";

        /// <summary>帶通（`voice_filter`／`voice_high_pass_hz`／`voice_low_pass_hz`；`MicListener` 的 `VoiceBand`）。</summary>
        public bool VoiceFilter = true;
        public double HighPassHz = VoiceBand.HIGH_PASS_HZ;
        public double LowPassHz = VoiceBand.LOW_PASS_HZ;

        /// <summary>
        /// 每一批新樣本（**帶通之後**、主執行緒）——喚醒網吃這個（`MicListener.onBlock`：先濾再量，
        /// 喚醒網、上行、電平看的是同一個訊號）。
        /// </summary>
        public Action<short[], int> Samples;

        private VoiceBand _band;
        private short[] _pcm;
        private bool _native;            // 走原生 `MicRecord`（Android）
        private AudioClip _clip;
        private string _device;
        private short[] _ring;
        private int _write;              // 下一個要寫進環形緩衝的位置
        private int _filled;             // 已經填過幾個樣本（<= ring.Length）
        private int _lastPos;            // 上一幀讀到 clip 的哪個位置
        private float[] _scratch;
        private float _level;
        private bool _permissionAsked;
        private float _diagAt;      // 診斷：每 3 秒印一次 Microphone 狀態（iOS 上「開麥成功但沒資料」要靠它分辨）
        private int _blocks;        // 診斷：已產出幾批樣本

        /// <summary>正在收音。</summary>
        public bool Running => _native || (_clip != null && Microphone.IsRecording(_device));

        /// <summary>最近一塊的 RMS（0..1；給節拍器的 `Level`，不是判斷）。</summary>
        public float Level => _level;

        /// <summary>權限被拒（Android）；呼叫端要把它寫進狀態列，不要靜靜地沒聲音。</summary>
        public bool Denied { get; private set; }

        /// <summary>收音失敗的原因（空＝沒問題）。</summary>
        public string Why { get; private set; } = string.Empty;

        /// <summary>開麥。回 false＝沒開成（看 <see cref="Why"/>／<see cref="Denied"/>）。</summary>
        public bool Start()
        {
            if (Running) return true;
            Why = string.Empty;
            if (!EnsurePermission()) return false;
            _ring ??= new short[SampleRate * RingSeconds];
            _scratch ??= new float[SampleRate];
            _pcm ??= new short[SampleRate];
            // 每次開麥一顆新的濾波器（IIR 狀態從零起；設定改了也在這裡生效）。
            _band = new VoiceBand(SampleRate, HighPassHz, LowPassHz, VoiceFilter);
#if UNITY_ANDROID && !UNITY_EDITOR
            return StartNative();
#else
            return StartUnity();
#endif
        }

        private bool StartNative()
        {
            var raw = BlueAgent.Screens.NativeShell.MicStart(Source, Effects);
            JObject o;
            try { o = new JObject(raw ?? "{}"); }
            catch (Exception e) { o = new JObject().Put("ok", false).Put("why", e.Message); }
            if (!o.OptBoolean("ok"))
            {
                Why = "開不了麥克風：" + o.OptString("why");
                Debug.LogWarning("[ears] " + Why);
                return false;
            }
            _native = true;
            EchoCancelled = o.OptBoolean("aec");
            Info = $"source={o.OptString("source")}／{o.OptString("available")}／掛上={o.OptString("attached")}";
            _write = 0;
            _filled = 0;
            Debug.Log($"[ears] 開麥（原生 AudioRecord）：{Info}／AEC={(EchoCancelled ? "有效" : "無")}／環形 {RingSeconds}s");
            return true;
        }

        private bool StartUnity()
        {
            // ⚠️ iOS 必須先開 AVAudioSession（PlayAndRecord ＋ setActive），否則
            //    `Microphone.Start` 不報錯、但 `GetPosition()` 永遠 0 ⇒ 一塊音訊都拿不到
            //    （2026-10-19 真機：`[wake] 探針：30 s 內 0 塊（沒在餵）`）。非 iOS 是空實作。
            AudioSession.Begin();
            if (Microphone.devices == null || Microphone.devices.Length == 0)
            {
                Why = "這台裝置沒有麥克風";
                Debug.LogWarning("[ears] " + Why);
                return false;
            }
            _device = Microphone.devices[0];
            // loop=true ＋ 1 秒一圈：我們自己輪詢位置、抄進環形緩衝（不靠 Unity 的長度語意）。
            _clip = Microphone.Start(_device, true, ClipSeconds, SampleRate);
            if (_clip == null)
            {
                Why = "Microphone.Start 回 null";
                Debug.LogWarning("[ears] " + Why);
                return false;
            }
            if (_clip.frequency != SampleRate)
            {
                // 有些裝置不給 16 kHz ⇒ 這條路會讓音高／內容全錯，寧可當場說出來。
                Debug.LogWarning($"[ears] 麥克風給的是 {_clip.frequency} Hz（要 {SampleRate}）"
                                 + "——橋那一側假設 16 kHz，先照送但要把這件事記著");
            }
            _lastPos = 0;
            EchoCancelled = false;
            Info = "unity Microphone：" + _device;
            _write = 0;
            _filled = 0;
            Debug.Log($"[ears] 開麥：{_device}（{_clip.frequency} Hz／{_clip.channels} 聲道／環形 {RingSeconds}s）");
            return true;
        }

        /// <summary>關麥（環形緩衝留著不清——下一輪的 `seed` 還要用；Kotlin 那一版也是這樣）。</summary>
        public void Stop()
        {
            if (_native)
            {
                _native = false;
                BlueAgent.Screens.NativeShell.MicStop();
                EchoCancelled = false;
                _level = 0f;
                Debug.Log("[ears] 關麥");
                return;
            }
            if (_clip == null) return;
            Microphone.End(_device);
            Destroy(_clip);
            _clip = null;
            _level = 0f;
            Debug.Log("[ears] 關麥");
        }

        /// <summary>
        /// 把「最新 <paramref name="count"/> 個樣本」抄進 <paramref name="dst"/>（回實際抄了幾個）。
        /// 不足（剛開麥、或環形還沒填滿）＝回 0——**不要補零硬送**：那會在橋側切出假的靜音段。
        /// </summary>
        public int Read(short[] dst, int count)
        {
            if (_ring == null || _filled < count) return 0;
            var start = ((_write - count) % _ring.Length + _ring.Length) % _ring.Length;
            for (var i = 0; i < count; i++) dst[i] = _ring[(start + i) % _ring.Length];
            return count;
        }

        /// <summary>觸發前那 <paramref name="ms"/> 毫秒（`seed` 那一塊）；沒有那麼多就給有的。</summary>
        public short[] Recent(int ms)
        {
            var want = Mathf.Clamp(SampleRate * ms / 1000, 0, _ring?.Length ?? 0);
            var have = Mathf.Min(want, _filled);
            var outBuf = new short[have];
            if (have == 0 || _ring == null) return outBuf;
            var start = ((_write - have) % _ring.Length + _ring.Length) % _ring.Length;
            for (var i = 0; i < have; i++) outBuf[i] = _ring[(start + i) % _ring.Length];
            return outBuf;
        }

        /// <summary>
        /// 閥門（`MicListener`：`if (paused && !cfg.bargeIn)` 那一段）：她在講／聾窗裡 ⇒ **什麼都不留**——
        /// 環形緩衝清空，閥門一開，seed／下一塊都只會是閥門之後的聲音。
        /// </summary>
        public void Drop()
        {
            _write = 0;
            _filled = 0;
        }

        /// <summary>開麥以來寫進環形緩衝的樣本總數（**只增不減**；`Drop` 也不歸零）——連續上行的游標用。</summary>
        public long Total { get; private set; }

        /// <summary>
        /// 從游標 <paramref name="cursor"/> 之後錄到的**全部**樣本（連續、不跳；最多環形那 3 秒），讀完把游標推到最新。
        ///
        /// 為什麼要它（2026-10-18）：<see cref="Read"/> 給的是「最新 N 個」——上一塊的 POST 慢於 0.5 秒時，
        /// 中間那段就**靜靜地掉了**（原 app 用 `uploadQ` 把它補回來）。橋側的 KWS／切句要的是連續的音，
        /// 名字被切掉一截就叫不醒。`Drop`（閥門）之後只會拿到閥門之後的聲音。
        /// </summary>
        public short[] ReadSince(ref long cursor)
        {
            if (_ring == null) return new short[0];
            var avail = (int)Math.Min(Total - cursor, _filled);
            if (avail <= 0)
            {
                if (cursor > Total) cursor = Total;
                return new short[0];
            }
            var outBuf = new short[avail];
            var start = ((_write - avail) % _ring.Length + _ring.Length) % _ring.Length;
            for (var i = 0; i < avail; i++) outBuf[i] = _ring[(start + i) % _ring.Length];
            cursor = Total;
            return outBuf;
        }

        /// <summary>把已經緩下來的音長（毫秒）——診斷用。</summary>
        public int BufferedMs => _filled * 1000 / SampleRate;

        private void Update()
        {
            if (_native)
            {
                var got = BlueAgent.Screens.NativeShell.MicDrain();
                if (got.Length == 0) return;
                _blocks++;
                Ingest(got, got.Length);
                return;
            }
            if (_clip == null) return;
            var pos = Microphone.GetPosition(_device);
            // 診斷（每 3 秒一行）：iOS 上「開麥成功但沒資料」時，唯一能分辨的是 **pos 有沒有在動**。
            //   實測症狀：pos 永遠 0（`IsRecording` 卻回 true）⇒ 不是取樣率問題、是音訊根本沒進來。
            _diagAt += Time.unscaledDeltaTime;
            if (_diagAt >= 3f)
            {
                _diagAt = 0f;
                Debug.Log($"[mic] pos={pos}／last={_lastPos}／rec={Microphone.IsRecording(_device)}／"
                          + $"clip={_clip.frequency}Hz×{_clip.channels}／blocks={_blocks}／環形={BufferedMs}ms"
                          + $"／音訊={AudioSession.RouteInfo()}");
            }
            if (pos < 0) return;
            var samples = _clip.samples;
            if (pos == _lastPos) return;
            var count = pos > _lastPos ? pos - _lastPos : samples - _lastPos;   // 繞圈了＝讀到尾、下一幀接頭
            if (count <= 0 || count > samples) { _lastPos = pos; return; }
            var take = Mathf.Min(count, _scratch.Length);
            if (!_clip.GetData(_scratch, _lastPos)) { _lastPos = pos; return; }
            for (var i = 0; i < take; i++) _pcm[i] = (short)Mathf.RoundToInt(Mathf.Clamp(_scratch[i], -1f, 1f) * 32767f);
            if (take > 0) { _blocks++; Ingest(_pcm, take); }
            _lastPos = pos;
        }

        private short[] _inject;
        private int _injectAt;

        /// <summary>
        /// 測試：把一個 16 kHz 單聲道 PCM16 WAV **按實際時間替換**麥克風的樣本（麥克風照開、節奏照它走）——
        /// 帶通、喚醒網、環形、上行全走原路。回 false＝讀不到／格式不對。
        /// </summary>
        public bool Inject(string path)
        {
#if !BA_SELFTEST
            Debug.LogWarning("[ears] 注入音訊只在 BA_SELFTEST 包裡（外部評審 v2 A2）");
            return false;
#else
            try
            {
                var bytes = System.IO.File.ReadAllBytes(path);
                var at = 12;
                while (at + 8 <= bytes.Length)
                {
                    var id = System.Text.Encoding.ASCII.GetString(bytes, at, 4);
                    var size = BitConverter.ToInt32(bytes, at + 4);
                    if (id == "data")
                    {
                        var n = Math.Min(size, bytes.Length - at - 8) / 2;
                        var pcm = new short[n];
                        Buffer.BlockCopy(bytes, at + 8, pcm, 0, n * 2);
                        _inject = pcm;
                        _injectAt = 0;
                        Debug.Log($"[ears] 注入音訊：{System.IO.Path.GetFileName(path)}（{n * 1000 / SampleRate} ms）");
                        return true;
                    }
                    at += 8 + size + (size & 1);
                }
            }
            catch (Exception e)
            {
                Debug.LogWarning("[ears] 注入音訊讀不到：" + e.Message);
            }
            return false;
#endif
        }

        /// <summary>一批新樣本：帶通（就地）→ 環形緩衝 → 電平 → 交給 <see cref="Samples"/>。</summary>
        private void Ingest(short[] pcm, int n)
        {
            if (_inject != null)
            {
                for (var i = 0; i < n; i++) pcm[i] = _injectAt < _inject.Length ? _inject[_injectAt++] : (short)0;
                if (_injectAt >= _inject.Length)
                {
                    _inject = null;
                    Debug.Log("[ears] 注入音訊：放完");
                }
            }
            _band?.Process(pcm, n);
            double acc = 0;
            for (var i = 0; i < n; i++)
            {
                var v = pcm[i];
                _ring[_write] = v;
                _write = (_write + 1) % _ring.Length;
                if (_filled < _ring.Length) _filled++;
                Total++;
                acc += (double)v * v;
            }
            _level = (float)(Math.Sqrt(acc / n) / 32768.0);
            Samples?.Invoke(pcm, n);
        }

        /// <summary>
        /// Android 的 `RECORD_AUDIO`：沒有就當場要（第一次會跳系統對話框）。
        /// ⚠️ 使用者按下「允許」之前回 false——呼叫端**不能**假裝在收音。
        /// </summary>
        private bool EnsurePermission()
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            if (!UnityEngine.Android.Permission.HasUserAuthorizedPermission(UnityEngine.Android.Permission.Microphone))
            {
                if (!_permissionAsked)
                {
                    _permissionAsked = true;
                    UnityEngine.Android.Permission.RequestUserPermission(UnityEngine.Android.Permission.Microphone);
                    Debug.Log("[ears] 要麥克風權限（RECORD_AUDIO）⇒ 請按允許，再按一次「呼喚」");
                }
                else
                {
                    Denied = true;
                    Debug.LogWarning("[ears] 麥克風權限還沒給 ⇒ 這一輪不收音");
                }
                Why = "還沒給麥克風權限";
                return false;
            }
#endif
            Denied = false;
            return true;
        }
    }
}
