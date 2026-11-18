using System.IO;
using BlueAgent.Logic;
using UnityEngine;

namespace BlueAgent.Next
{
    /// <summary>
    /// 語音簡訊的錄音器（B1）：按一下開始、再按一下停止 ⇒ 回一段 **16 kHz／單聲道／16-bit WAV**。
    ///
    /// 為什麼不用舊殼的 `MicCapture`：那一支是為了「一直聽」設計的（原生 AudioRecord＋AEC＋環形緩衝、
    /// 還牽著 `EarsLink`）。語音簡訊只要「按住的這幾秒」，用 Unity 的 `Microphone` 就夠，
    /// 也不必把舊橋那條線拉進來。
    ///
    /// ⚠️ 取樣率：麥克風不一定給 16 kHz（實測有些裝置給 44.1/48 kHz）⇒ 這裡做一次線性重採樣到 16 kHz，
    /// 否則 SenseVoice 會聽成別的語言（音高全錯）。
    /// </summary>
    public sealed class NextVoice : MonoBehaviour
    {
        public const int SampleRate = 16000;
        public const int MaxSeconds = 60;

        private string _device;
        private AudioClip _clip;
        private float _startedAt;
        private bool _fromFile;
        private float _fileSeconds;

        public bool Recording { get; private set; }

        /// <summary>開不了麥的原因（空＝沒問題）。</summary>
        public string Why { get; private set; } = "";

        /// <summary>已錄幾秒（錄音中才有意義）。</summary>
        /// <remarks>
        /// ⚠️ **秒數一律以「實際錄到的樣本數」為準**，不拿牆鐘時間算。
        /// 2026-10-29 踩過：這裡原本回 `Time.realtimeSinceStartup - _startedAt`，
        /// 而 `Stop()` 裁資料用的是 `Microphone.GetPosition()`（實際樣本數）——
        /// 兩套來源會脫節，出現「按住 3 秒卻顯示 60.0s」（牆鐘走到 60 秒上限）。
        /// 現在兩邊都走 <see cref="RecordedFrames"/>，顯示與內容永遠一致。
        /// </remarks>
        public float Seconds
        {
            get
            {
                if (!Recording) return 0f;
                if (_fromFile) return _fileSeconds;
                return RecordedFrames() / (float)ClipFrequency;
            }
        }

        /// <summary>錄音 clip 的實際取樣率（裝置不一定給 16 kHz）。</summary>
        private int ClipFrequency
        {
            get
            {
                if (_clip == null) return SampleRate;
                return _clip.frequency > 0 ? _clip.frequency : SampleRate;
            }
        }

        /// <summary>實際錄進去的樣本數（`GetPosition`）；拿不到就回 0，不猜。</summary>
        /// <remarks>
        /// 兩個上限都要卡：
        /// ① `clip.samples`（緩衝全長＝MaxSeconds × 取樣率）——防止越界取值。
        /// ② **牆鐘時間**——不可能錄到比經過的時間更長的音訊；
        ///    `GetPosition` 在部分裝置上會回整個緩衝長度（曾裁出 60 秒、畫面顯示 60.0s，
        ///    實際只說了幾秒）。留一點餘量（+0.5 秒）給緩衝延遲。
        /// </remarks>
        private int RecordedFrames()
        {
            if (_fromFile || _clip == null || string.IsNullOrEmpty(_device)) return 0;
            int position;
            try
            {
                position = Microphone.GetPosition(_device);
            }
            catch
            {
                return 0;
            }
            // 範圍規則（未錄滿的 0、緩衝全長、牆鐘＋餘量）在 Logic 的 VoiceFrames，dotnet test 釘著
            return VoiceFrames.Recorded(position, _clip.samples, Time.realtimeSinceStartup - _startedAt,
                                        MaxSeconds, ClipFrequency);
        }

        /// <summary>開錄。**不要叫 `Start()`**（見下面那則警告）。</summary>
        /// <remarks>
        /// ⚠️⚠️ **這裡原本叫 `Start()` ⇒ Unity 把它當成生命週期回調自動呼叫了**
        /// （2026-10-29 owner：「app 一啟動就會拉起麥克風，這是不正確的」）。
        /// 實測：app 開起來、**一根手指都沒碰**，麥克風就已經在錄
        /// （logcat `[next-voice] 録音開始`；appops `RECORD_AUDIO … (running)`）。
        /// Unity 的訊息機制**只看方法名、不看回傳型別** ⇒ `public bool Start()` 也會被呼叫。
        /// （呼叫鏈裡看不到 `HoldDown`／`OnPointerDown` 就是這個原因——它根本沒走那條路。）
        /// <br/>
        /// ⇒ 改成 `BeginRecording()`，跟生命週期回調名字徹底分開。
        /// </remarks>
        public bool BeginRecording()
        {
            if (Recording) return true;
            Why = "";
            Debug.Log("[next-voice] Start（録音要求）");

#if BA_SELFTEST
            // 驗收鉤子（只在 BA_SELFTEST=1 的包裡）：模擬器沒有麥克風輸入，
            // 把要送的 wav 放到下面任一個位置 ⇒ 按錄音就送那一顆。
            // ① 外部 files（＝`persistentDataPath`）② 內部 `/data/data/<pkg>/files`（`run-as` 可寫的那個）。
            //    實測：`adb push` 到外部目錄，app 用 scoped storage 看不到；`run-as` 又寫不進 /storage
            //    ⇒ 走內部路徑這一條才穩。
            var candidates = new[]
            {
                Path.Combine(Application.persistentDataPath, "inject.wav"),
                "/data/data/" + Application.identifier + "/files/inject.wav",
            };
            foreach (var candidate in candidates)
            {
                if (!File.Exists(candidate)) continue;
                _fromFile = true;
                _fileSeconds = 3.0f;
                _startedAt = Time.realtimeSinceStartup;
                Recording = true;
                Debug.Log("[next-voice] 測試注入：" + candidate);
                return true;
            }
            Debug.Log("[next-voice] selftest 檢查：找不到 inject.wav（找了 "
                      + string.Join("、", candidates) + "）");
#endif

            if (!EnsurePermission()) return false;
            if (Microphone.devices == null || Microphone.devices.Length == 0)
            {
                Why = NextStrings.Get("mic_none");
                return false;
            }
            _device = Microphone.devices[0];
            _clip = Microphone.Start(_device, false, MaxSeconds, SampleRate);
            if (_clip == null)
            {
                Why = NextStrings.Get("mic_start_fail");
                return false;
            }
            _fromFile = false;
            _startedAt = Time.realtimeSinceStartup;
            Recording = true;
            Debug.Log("[next-voice] 録音開始：" + _device + $"（{_clip.frequency} Hz／{_clip.channels}ch）");
            return true;
        }

        /// <summary>停止錄音並回 WAV（`seconds` 是實際長度；失敗回 null）。</summary>
        public byte[] Stop(out float seconds)
        {
            seconds = 0f;
            if (!Recording) return null;
            Recording = false;

#if BA_SELFTEST
            if (_fromFile)
            {
                _fromFile = false;
                var path = Path.Combine(Application.persistentDataPath, "inject.wav");
                if (!File.Exists(path)) { Why = "inject.wav missing"; return null; }
                var raw = File.ReadAllBytes(path);
                seconds = Mathf.Max(0.1f, (raw.Length - 44) / (float)(SampleRate * 2));
                Debug.Log($"[next-voice] 注入送出：{raw.Length} bytes／{seconds:0.0}s");
                return raw;
            }
#endif

            var position = RecordedFrames();
            Microphone.End(_device);

            var clip = _clip;
            _clip = null;
            if (clip == null) { Why = NextStrings.Get("record_fail"); return null; }

            var frequency = clip.frequency > 0 ? clip.frequency : SampleRate;
            var channels = clip.channels > 0 ? clip.channels : 1;
            // 樣本數以 `RecordedFrames()` 為準（已卡在 0..緩衝全長，見 VoiceFrames）：`GetPosition` 異常時
            // 曾裁出整個 60 秒緩衝，畫面顯示 60.0s、實際只說了幾秒。
            var frames = position;
            if (frames <= 0) { Destroy(clip); Why = NextStrings.Get("record_empty"); return null; }

            var samples = new float[frames * channels];
            clip.GetData(samples, 0);
            Destroy(clip);

            // 多聲道 → 單聲道
            var mono = new float[frames];
            for (var i = 0; i < frames; i++)
            {
                var sum = 0f;
                for (var c = 0; c < channels; c++) sum += samples[i * channels + c];
                mono[i] = sum / channels;
            }

            var resampled = Resample(mono, frequency, SampleRate);
            var pcm = new short[resampled.Length];
            for (var i = 0; i < resampled.Length; i++)
            {
                var v = Mathf.Clamp(resampled[i], -1f, 1f);
                pcm[i] = (short)Mathf.RoundToInt(v * 32767f);
            }
            seconds = pcm.Length / (float)SampleRate;
            Debug.Log($"[next-voice] 録音終了：{frequency} Hz／{channels}ch ⇒ 16 kHz mono、{seconds:0.0}s");
            return WavBytes.Encode(pcm, SampleRate);
        }

        /// <summary>線性重採樣（夠用；語音簡訊不是音樂）。</summary>
        private static float[] Resample(float[] input, int from, int to)
        {
            if (input == null || input.Length == 0) return new float[0];
            if (from == to || from <= 0) return input;
            var length = Mathf.Max(1, Mathf.RoundToInt(input.Length * (float)to / from));
            var output = new float[length];
            var ratio = (input.Length - 1f) / Mathf.Max(1, length - 1);
            for (var i = 0; i < length; i++)
            {
                var pos = i * ratio;
                var index = Mathf.FloorToInt(pos);
                var frac = pos - index;
                var a = input[index];
                var b = index + 1 < input.Length ? input[index + 1] : a;
                output[i] = a + (b - a) * frac;
            }
            return output;
        }

        /// <summary>麥克風權限（Android 6+／iOS 都要）。權限剛請求時回 false，請使用者再按一次。</summary>
        private bool EnsurePermission()
        {
#if UNITY_ANDROID && !UNITY_EDITOR
            if (!UnityEngine.Android.Permission.HasUserAuthorizedPermission(UnityEngine.Android.Permission.Microphone))
            {
                UnityEngine.Android.Permission.RequestUserPermission(UnityEngine.Android.Permission.Microphone);
                Why = NextStrings.Get("mic_permission");
                return false;
            }
#elif UNITY_IOS && !UNITY_EDITOR
            if (!Application.HasUserAuthorization(UserAuthorization.Microphone))
            {
                Application.RequestUserAuthorization(UserAuthorization.Microphone);
                Why = NextStrings.Get("mic_permission");
                return false;
            }
#endif
            return true;
        }
    }
}
