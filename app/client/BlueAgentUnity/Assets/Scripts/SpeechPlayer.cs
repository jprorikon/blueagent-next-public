using System;
using System.Collections.Generic;
using BlueAgent.Logic.Engine;
using UnityEngine;

namespace BlueAgent.Spike
{
    /// <summary>
    /// **播放橋送來的那一句**（`speech.audio` 是 base64 的 WAV）＋ 順手當**量測音量的來源**。
    ///
    /// 這一支是**集成層**（各平台各一份）：解碼與電平的算術在共用邏輯層的 <see cref="WavAudio"/>，
    /// 這裡只做 Unity 才有的兩件事——把樣本塞進 `AudioClip` 播出來、以及從「正在播的樣本」算電平。
    ///
    /// 為什麼要用 `OnAudioFilterRead` 而不是看檔案：那是**播放中的樣本**（跟耳朵聽到的一致），
    /// 用它算出來的電平就是引擎 `LipSyncController` 量測那條路要吃的東西。
    /// ⚠️ `OnAudioFilterRead` 在**音訊執行緒**上跑：只能算數字、**不要碰 Unity API**（也不要配置記憶體）。
    /// </summary>
    public class SpeechPlayer : MonoBehaviour
    {
        /// <summary>
        /// 把這一支**當成引擎的 `AudioLevelAnalyzer`** 交出去的那個實例。
        ///
        /// ⚠️ 為什麼要多這一層：C# 不能同時繼承 `MonoBehaviour` 與 `AudioLevelAnalyzer`（單一繼承）；
        /// 而 `AudioLevelAnalyzer` 是**上游的形狀**（抽象類別），不該為了這裡改成介面（會漂掉對拍）。
        /// ⇒ 用**組合**：`MonoBehaviour` 負責播放，這一顆負責回答引擎問的電平。
        /// </summary>
        private sealed class MeasuredSource : AudioLevelAnalyzer
        {
            private readonly SpeechPlayer _player;
            public MeasuredSource(SpeechPlayer player) { _player = player; }
            public override bool IsAvailable() => _player.IsPlaying;
            public override double GetLevel() => _player.LastLevel;
            public override double? GetPeak() => _player.LastPeak > 0 ? _player.LastPeak : (double?)null;
            public override void Reset() => _player.Reset();
        }

        private MeasuredSource _analyzer;

        /// <summary>交給引擎的「量測音量來源」（見 <see cref="MeasuredSource"/> 的說明）。</summary>
        public AudioLevelAnalyzer Analyzer => _analyzer ??= new MeasuredSource(this);

        /// <summary>電平的一次極衰減（每幀；跟 `LipSyncController` 的 attack／release 不同，這裡只求單調）。</summary>
        private const float LevelDecayPerFrame = 0.85f;

        private AudioSource _source;
        private string _lastName = "";

        /// <summary>現在播到第幾秒（沒在播＝0）。嘴型腳本要靠它對位。</summary>
        public double PlaybackSeconds
        {
            get
            {
                // ⚠️ 沒有 clip 時**不要去問 `AudioSource.time`**：Unity 會每秒噴一次
                //    「Attempting to get `time` on an audio source that has a resource that is not a clip」，
                //    把 logcat 洗掉（真機只有 logcat 這條通道，被洗掉就等於瞎了）。
                if (_source == null || _source.clip == null) return 0.0;
                if (!_source.isPlaying) return 0.0;
                // ⚠️ **嘴要用「聽到的」時間，不是「餵進去的」時間**（2026-10-12 真機 owner 回報
                //    「音訊和嘴形不同步」）：`AudioSource.time` 是**已經餵進輸出緩衝**的位置，
                //    真正從喇叭出來還要等一個輸出緩衝（DSP buffer × numBuffers ÷ 輸出取樣率，
                //    Android 常見 80～200 ms）⇒ 直接拿 `time` 取樣嘴型，嘴會**領先聲音**那麼多。
                //    所以這裡減掉推導出來的延遲；`OutputLatencySeconds` 可用
                //    `--ef mouthLatencyMs <毫秒>`（真機）／`BA_MOUTHLATENCYM_S`（編輯器）覆寫，
                //    調的時候不必重建 APK。
                var seconds = _source.time - OutputLatencySeconds;
                return seconds > 0 ? seconds : 0.0;
            }
        }

        /// <summary>
        /// 「聽到的」比 `AudioSource.time` 晚多少秒（輸出延遲補償）。
        ///
        /// 優先序：① `mouthLatencyMs` extra／環境變數（校正用，真機不必重建）② 由音訊設定推導
        /// （`GetDSPBufferSize` × `outputSampleRate`，**不是寫死的常數**）。
        /// </summary>
        public double OutputLatencySeconds { get; private set; }

        /// <summary>這個值是不是被額外參數覆寫過（HUD／log 要看得出「現在用的是哪一個」）。</summary>
        public bool LatencyOverridden { get; private set; }

        private double ResolveLatencySeconds()
        {
            if (DeviceIntent.TryFloat("mouthLatencyMs", "BA_MOUTH_LATENCY_MS", out var ms, "engine"))
            {
                LatencyOverridden = true;
                return Math.Max(0.0, ms / 1000.0);
            }
            AudioSettings.GetDSPBufferSize(out var bufferLength, out var numBuffers);
            var rate = AudioSettings.outputSampleRate;
            if (rate <= 0 || bufferLength <= 0 || numBuffers <= 0) return 0.0;
            return (double)bufferLength * numBuffers / rate;
        }

        // ⚠️ **沒有 clip 就不算在播**（2026-10-18 真機）：這顆 `AudioSource` 跟 `OnAudioFilterRead`／`AudioListener`
        //    同一個物件，Unity 會回 `isPlaying=true`（clip／resource 都是 null）⇒ `Speaking` 永遠真、耳朵的閥門永遠關。
        public bool IsPlaying => _source != null && _source.clip != null && _source.isPlaying;

        /// <summary>診斷：這顆 `AudioSource` 現在放的是什麼（閥門卡住時看）。</summary>
        public string Describe() => _source == null ? "source=null"
            : $"clip={(_source.clip != null ? _source.clip.name : "null")}／resource={(_source.resource != null ? _source.resource.GetType().Name + ":" + _source.resource.name : "null")}"
              + $"／loop={_source.loop}／sources={(_meter != null ? 1 : 0)}／last={_lastName}";

        /// <summary>播放音量（0–1；設定頁「回覆音量」——真相 `MediaPlayer.setVolume(vol, vol)`）。</summary>
        public float Volume
        {
            get => _source != null ? _source.volume : 1f;
            set { if (_source != null) _source.volume = Mathf.Clamp01(value); }
        }

        /// <summary>最近一次量到的電平／峰值（給 HUD 看的）。</summary>
        public double LastLevel { get; private set; }
        public double LastPeak { get; private set; }

        /// <summary>
        /// 電錶：**跟 `AudioSource` 住同一個物件**的 `OnAudioFilterRead`。
        ///
        /// 為什麼要拆成獨立元件（2026-10-19 iOS 模擬器實測）：`OnAudioFilterRead` 只認「同物件的
        /// AudioSource／AudioListener」，而 Unity 禁止同一個 MonoBehaviour 被兩者同時驅動。
        /// 播放器（`SpeechPlayer`）住在相機上、相機有 `AudioListener` ⇒ 電錶只能搬去只放
        /// `AudioSource` 的子物件。音訊執行緒讀寫，所以兩個值是 `volatile float`。
        /// </summary>
        private sealed class VoiceMeter : MonoBehaviour
        {
            public volatile float Level;
            public volatile float Peak;

            private void OnAudioFilterRead(float[] data, int channels)
            {
                var (level, peak) = WavAudio.Levels(data);   // 純算術：不碰 Unity API、不配置記憶體
                Level = (float)level;
                Peak = (float)peak;
            }
        }

        private VoiceMeter _meter;

        private void Awake()
        {
            _analyzer = new MeasuredSource(this);
            _source = EnsureVoiceSource(out _meter);
            _source.playOnAwake = false;
            _source.spatialBlend = 0f;                  // 2D（立繪的聲音不該有方向感）

            // ⚠️ **沒有 `AudioListener` ＝ 沒有聲音**（2026-10-12 真機踩到，症狀極具誤導性）：
            //    `AudioSource.Play()` 照樣成立（`isPlaying=true`、`time` 照走、HUD／心跳看起來一切正常），
            //    但**沒有任何聲音被送出**——因為這台裝置上沒有「耳朵」在聽。
            //    場景是程式建的（`HeadlessBuild.CreateSpikeScene` 的 `new GameObject("Main Camera", typeof(Camera))`），
            //    而 Unity **只會幫「編輯器選單建的相機」自動掛** `AudioListener` ⇒ 程式建的就沒有。
            //    ⇒ 這裡**當場補一個並大聲說出來**（不要讓它安靜地沒聲音）。
            // ⚠️⚠️ **但絕對不能補在自己身上**（2026-10-19 iOS 模擬器實測）：
            //    這一支有 `OnAudioFilterRead`（＝電錶），Unity 規定「同一個 `MonoBehaviour` 的
            //    `OnAudioFilterRead` 只能由**一個** `AudioSource` 或 `AudioListener` 驅動」
            //    ⇒ 同一個 GameObject 上同時有 AudioSource 與 AudioListener 時直接報錯：
            //    `GameObject has multiple AudioSources and/or AudioListeners attached …`
            //    （症狀：整片紅字蓋住畫面，而且語音那半根本沒初始化）。
            //    ⇒ 耳朵要**另起一個乾淨的 GameObject**，離播放器越遠越好。
            if (FindFirstObjectByType<AudioListener>() == null)
            {
                var ear = new GameObject("Audio Ear");
                ear.transform.SetParent(transform.root, false);
                ear.AddComponent<AudioListener>();
                Debug.LogWarning("[engine] 場景裡沒有 AudioListener ⇒ 另建一個 Audio Ear"
                                 + "（不可以掛在播放器身上：會跟 OnAudioFilterRead 打架）");
            }

            AudioSettings.GetDSPBufferSize(out var bufferLength, out var numBuffers);
            OutputLatencySeconds = ResolveLatencySeconds();
            Debug.Log($"[engine] 嘴型時基：輸出延遲補償 {OutputLatencySeconds * 1000.0:F0} ms"
                      + $"（{(LatencyOverridden ? "由 mouthLatencyMs 指定" : "推導自 DSP 緩衝")}；"
                      + $"DSP 緩衝 {bufferLength}×{numBuffers} @ {AudioSettings.outputSampleRate}Hz）");
        }

        /// <summary>
        /// 播放用的 `AudioSource` ＋ 同一物件上的電錶（`VoiceMeter`）。
        ///
        /// 為什麼要**另開一個子物件**：見 `Awake` 裡那段（`OnAudioFilterRead` 與 `AudioListener` 不能同居）。
        /// 子物件只放 `AudioSource` ＋ `VoiceMeter`，父物件（相機）那邊照樣可以有 `AudioListener`，
        /// 兩邊互不干涉。實測 2026-10-19 iOS 模擬器：改完那兩條紅字消失、聲音與嘴型都正常。
        /// </summary>
        private AudioSource EnsureVoiceSource(out VoiceMeter meter)
        {
            var child = transform.Find("Voice");
            if (child == null)
            {
                var go = new GameObject("Voice");
                go.transform.SetParent(transform, false);
                child = go.transform;
            }
            var source = child.GetComponent<AudioSource>();
            if (source == null) source = child.gameObject.AddComponent<AudioSource>();
            meter = child.GetComponent<VoiceMeter>();
            if (meter == null) meter = child.gameObject.AddComponent<VoiceMeter>();
            return source;
        }

        /// <summary>播一段 base64 的 WAV（橋的 `speech.audio`）。空／壞掉的就靜靜地不播（回 false）。</summary>
        public bool PlayBase64(string base64, string name = "")
        {
            if (string.IsNullOrEmpty(base64)) return false;
            byte[] bytes;
            try
            {
                bytes = Convert.FromBase64String(base64);
            }
            catch (FormatException e)
            {
                Debug.LogWarning($"[engine] speech.audio 不是合法的 base64：{e.Message}");
                return false;
            }
            return PlayWav(bytes, name);
        }

        /// <summary>播一段 WAV 位元組（`startSeconds`＞0 ⇒ 從那裡接著播：背景再生から戻った續き）。</summary>
        public bool PlayWav(byte[] wav, string name = "", float startSeconds = 0f)
        {
            WavAudio.Clip clip;
            try
            {
                clip = WavAudio.Parse(wav);
            }
            catch (FormatException e)
            {
                Debug.LogWarning($"[engine] 解不開這一段 WAV：{e.Message}");
                return false;
            }

            Stop();
            var clipName = string.IsNullOrEmpty(name) ? "speech" : name;
            var audioClip = AudioClip.Create(clipName, clip.FrameCount, clip.Channels, clip.SampleRate, false);
            if (!audioClip.SetData(clip.Samples, 0))
            {
                Debug.LogWarning("[engine] AudioClip.SetData 失敗（聲道／格數對不上？）");
                return false;
            }
            // 播之前把輸出拉到喇叭（Unity 開輸出時會重設 session；不補就從聽筒出來）。
            AudioSession.ForceSpeaker();
            _source.clip = audioClip;
            _source.Play();
            if (startSeconds > 0f && startSeconds < audioClip.length) _source.time = startSeconds;
            _wav = wav;
            _lastName = clipName;
            Debug.Log($"[engine] 播一句：{clipName}（{clip.Seconds:F2} 秒／{clip.SampleRate}Hz／{clip.Channels} 聲道）");
            return true;
        }

        /// <summary>
        /// 途中まで鳴っていた一句の WAV（背景に回るとき原生へ渡す；無ければ null）。
        /// ⚠️ `OnApplicationPause(true)` の時點で Unity はもう音を止めていて **`isPlaying` は false**（2026-10-19 真機：
        /// 鳴っていた一句が渡されずに消えた）⇒ 「位置が 0 より先」で判定する（鳴り終わった clip は位置が 0 に戻る）。
        /// </summary>
        public byte[] CurrentWav =>
            _source != null && _source.clip != null && (_source.isPlaying || _source.timeSamples > 0) ? _wav : null;

        /// <summary>その一句の再生位置（秒；出力遅延の補正なし＝原生側でそこから續ける位置）。</summary>
        public double RawSeconds => _source != null && _source.clip != null ? _source.time : 0.0;

        private byte[] _wav;

        public void Stop()
        {
            _wav = null;
            if (_source == null) return;
            if (_source.isPlaying) _source.Stop();
            if (_source.clip != null)
            {
                Destroy(_source.clip);
                _source.clip = null;
            }
        }

        private void Update()
        {
            // 把音訊執行緒算好的值搬過來；沒在播就慢慢收乾（不要突然跳 0，那會讓嘴抽一下）。
            var threadLevel = _meter != null ? _meter.Level : 0f;
            var threadPeak = _meter != null ? _meter.Peak : 0f;
            LastLevel = Math.Max(threadLevel, LastLevel * LevelDecayPerFrame);
            LastPeak = Math.Max(threadPeak, LastPeak * LevelDecayPerFrame);
            if (!IsPlaying)
            {
                LastLevel *= LevelDecayPerFrame;
                LastPeak *= LevelDecayPerFrame;
            }
        }

        /// <summary>電平歸零（換來源／換句子的時候叫）。</summary>
        public void Reset()
        {
            LastLevel = 0;
            LastPeak = 0;
            if (_meter != null)
            {
                _meter.Level = 0f;
                _meter.Peak = 0f;
            }
        }
    }
}
