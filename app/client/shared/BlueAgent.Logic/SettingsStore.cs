using System;
using System.Collections.Generic;

namespace BlueAgent.Logic
{
    /// <summary>
    /// What the phone shell persists.
    ///
    /// ＝ `blueagent/live2d/android/kmp/src/commonMain/.../SettingsStore.kt` 的**逐行移植**。
    /// 等價性由 `fixtures/settings_store.json`（Kotlin 那一側機器產生的真值）逐條對拍，不靠「看起來差不多」。
    ///
    /// Key names follow the Python shell's `config.json` wherever a key already had a meaning
    /// (`output_language`, `mic_mode`, `vad_min_rms`), so the two trees stay comparable while this one is
    /// being built. Keys that only exist on the phone (the gateway credentials, the STT host) are new and
    /// documented at the property.
    ///
    /// 2026-10-03：`tts_url`／`tts_enabled`／`tts_text_lang`／`tts_remote_root` 四個鍵**已移除**——
    /// TTS 走橋側之後手機不再直連合成服務（`TtsClient`／`VoiceRefs`／`VoicePacks.pick` 同時刪掉）。
    ///
    /// 2026-10-02 搬進 `commonMain`（兩端共用）：後端由呼叫端注入（安卓＝`SharedPreferences`、
    /// iOS＝`UserDefaults`，見 <see cref="ISettingsBackend"/>），**鍵名／預設值／夾界／遷移規則全部留在這裡**——
    /// 平台那一層不知道任何一個鍵的意義。
    ///
    /// ⚠️ 移植時的三條**不能「改善」**的地方（fixture 會當場抓到）：
    /// <list type="bullet">
    /// <item>浮點設定（`Double` 屬性）走的是後端的 **Float**：寫入 `value.toFloat()`、讀出
    ///   `getFloat(key, def.toFloat()).toDouble()` ⇒ 1.8 讀回來是 1.7999999523162842（**不是** 1.8）。
    ///   這是既有行為、不是搬遷造成的。</item>
    /// <item>`micMode` 的讀取把舊值 `ptt` 映射成 `wake`（UI 已經沒有那個模式了）。</item>
    /// <item>`bridgeSession` 的**遷移**：先讀新鍵，讀不到才回頭看 iOS 舊版的 `session` 鍵。</item>
    /// </list>
    /// </summary>
    public sealed class SettingsStore
    {
        private readonly ISettingsBackend _backend;

        public SettingsStore(ISettingsBackend backend)
        {
            _backend = backend;
        }

        private string Str(string key, string def) => _backend.GetString(key, def);
        private void Put(string key, string value) => _backend.PutString(key, value);
        private int Int(string key, int def) => _backend.GetInt(key, def);
        private void Put(string key, int value) => _backend.PutInt(key, value);
        private bool Flag(string key, bool def) => _backend.GetBoolean(key, def);
        private void Put(string key, bool value) => _backend.PutBoolean(key, value);
        /// <summary>`backend.getFloat(key, def.toFloat()).toDouble()`（逐條照 Kotlin 寫，含那段精度損失）。</summary>
        private double Dbl(string key, double def) => (double)_backend.GetFloat(key, (float)def);
        /// <summary>`backend.putFloat(key, value.toFloat())`。</summary>
        private void Put(string key, double value) => _backend.PutFloat(key, (float)value);

        /// <summary>Stored here like the Python shell stored it in its own config; never leaves the phone.</summary>
        public string Character
        {
            get => Str("character", "aster");
            set => Put("character", value);
        }

        /// <summary>
        /// 舞台背景的 pack id；`''` ＝ 無背景（頁面畫回斜線紋底）——**`''` 是一個真的選項**，不是
        /// 「沒東西可畫」。時段（slot）**不存**：切點寫在殼裡、由日本時間現算，頁面只收場景。
        /// </summary>
        public string Background
        {
            get => Str("background", "");
            set => Put("background", value);
        }

        /// <summary>
        /// 隨機模式（owner 2026-10-06）：開著時，**每次時段切換點**都由殼自己抽一個新的場景
        /// （`''`＝無背景不在池子裡）。存的只是「這個開關開著」——抽出來的場景仍然寫進 <see cref="Background"/>，
        /// 所以關掉之後畫面不會跳。
        /// </summary>
        public bool BackgroundRandom
        {
            get => Flag("background_random", false);
            set => Put("background_random", value);
        }

        /// <summary>realtime (hands-free) | typeless</summary>
        public string Mode
        {
            get => Str("mode", "realtime");
            set => Put("mode", value);
        }

        /// <summary>chat | work</summary>
        public string WorkMode
        {
            get => Str("work_mode", "chat");
            set => Put("work_mode", value);
        }

        /// <summary>whisper language hint</summary>
        public string InputLanguage
        {
            get => Str("input_language", "ja");
            set => Put("input_language", value);
        }

        /// <summary>what gets spoken</summary>
        public string OutputLanguage
        {
            get => Str("output_language", "ja");
            set => Put("output_language", value);
        }

        /// <summary>
        /// 介面語言（owner 2026-10-03）：`ja`／`zh-Hant`／`zh-Hans`。
        ///
        /// **空字串＝跟隨系統語言**（頁面讀 `navigator.language`；認不得就退回 `zh-Hant`）。
        /// 字全部在 `assets/webui/i18n.js`（一份字典、三種語言）——這裡只存「用哪一種」。
        /// **只影響畫面上的 UI 文字**：她的台詞／記憶／語音／貼圖／角色名一概不動。
        /// </summary>
        public string UiLocale
        {
            get => Str("ui_locale", "");
            set => Put("ui_locale", KotlinText.Trim(value));
        }

        public int OutputVolume
        {
            get => Int("output_volume", 100);
            set => Put("output_volume", CoerceIn(value, 0, 100));
        }

        public bool SpeakReplies
        {
            get => Flag("speak_replies", true);
            set => Put("speak_replies", value);
        }

        /// <summary>
        /// How a turn is opened: `wake` (say my name) or `toggle` (every pause closes a turn).
        ///
        /// There was a third value, `ptt` — press and hold. It is gone with the PC shell's talk button:
        /// on a phone the microphone is opened by the wake phrase, so a stored `ptt` reads back as `wake`
        /// rather than resurrecting a mode the UI no longer has.
        /// </summary>
        public string MicMode
        {
            get
            {
                var it = Str("mic_mode", "wake");
                return it == "ptt" ? "wake" : it;
            }
            set => Put("mic_mode", value);
        }

        /// <summary>Wake phrases for the current character, comma-separated. Empty = the built-in name.</summary>
        public string WakeWord
        {
            get => Str("wake_word", "");
            set => Put("wake_word", KotlinText.Trim(value));
        }

        /// <summary>
        /// The wake net (a small convolutional net on the phone, not whisper's transcript): on by default,
        /// because that is the whole point of it.
        /// </summary>
        public bool KwsEnabled
        {
            get => Flag("kws.enabled", true);
            set => Put("kws.enabled", value);
        }

        /// <summary>Score a frame must reach. The default is the blob's own operating point (0.5 at shipping).</summary>
        public float KwsThreshold
        {
            get => _backend.GetFloat("kws.threshold", 0f);
            set => _backend.PutFloat("kws.threshold", value);
        }

        /// <summary>
        /// Let her own name reach the detector while she is talking, and keep the post-reply deaf window short.
        ///
        /// On by default: the platform echo canceller is attached on this phone (the log's `mic` line says
        /// `NS+AEC`), so her own tail is cancelled and the microphone does not have to be shut. Off restores the
        /// 900 ms shut-mic behaviour that was measured safe with no echo canceller.
        /// </summary>
        public bool BargeIn
        {
            get => Flag("kws.bargeIn", true);
            set => Put("kws.bargeIn", value);
        }

        public int KwsDecideEvery
        {
            get => Int("kws.decideEvery", 1);
            set => Put("kws.decideEvery", value);
        }

        /// <summary>
        /// Frames over the wake threshold before a trigger counts. **Baseline 3** (owner 2026-10-18; was 2 = the
        /// original app's default). Changed together with `BgWake.Config.consecutive` and the fixture's `defaults` case.
        /// </summary>
        public int KwsConsecutive
        {
            get => Int("kws.consecutive", 3);
            set => Put("kws.consecutive", CoerceIn(value, 1, 10));
        }

        /// <summary>Quiet time after an ACCEPTED trigger. A rejected one is not cooled down (see `WakeKws.onBlock`).</summary>
        public int KwsCooldownMs
        {
            get => Int("kws.cooldownMs", 1500);
            set => Put("kws.cooldownMs", CoerceIn(value, 0, 10000));
        }

        /// <summary>
        /// ①聲紋閘（認人）——**觸發之後、叫醒之前**問一次「這是不是他的聲音」。
        ///
        /// 預設 on：這是手機上唯一能便宜攔下誤觸的地方（她自己的 TTS、電視、別人）。關掉＝每次觸發都醒，
        /// 用來親耳聽誤報量（A/B 的耳朵組）。判人的分數由 <see cref="SpkSherpa"/>（沒選＝APK 內建的學生 blob）算。
        /// </summary>
        public bool SpeakerGate
        {
            get => Flag("kws.speaker", true);
            set => Put("kws.speaker", value);
        }

        /// <summary>
        /// 學生 blob 的門檻覆蓋（0＝用 blob 自帶的操作點）。
        ///
        /// ⚠️ 兩顆引擎的分數不在同一個空間：設了 <see cref="SpkSherpa"/> 時這一格不生效，那一行寫的是
        /// <see cref="SpkSherpaThreshold"/>（見 `SpeakerBars`）。
        /// </summary>
        public float SpeakerThreshold
        {
            get => _backend.GetFloat("kws.speaker.threshold", 0f);
            set => _backend.PutFloat("kws.speaker.threshold", value);
        }

        /// <summary>閘門用哪一顆 sherpa 模型（質心檔名，檔在 files/models/）；空＝內建學生 blob。</summary>
        public string SpkSherpa
        {
            get => Str("kws.spkSherpa", "");
            set => Put("kws.spkSherpa", value);
        }

        /// <summary>那一顆引擎的門檻（0＝用質心檔裡烤好的值）。</summary>
        public float SpkSherpaThreshold
        {
            get => _backend.GetFloat("kws.spkSherpa.threshold", 0f);
            set => _backend.PutFloat("kws.spkSherpa.threshold", value);
        }

        /// <summary>onnxruntime 的 execution provider（cpu／nnapi／xnnpack；不支援會自己退回 cpu）。</summary>
        public string SpkSherpaProvider
        {
            get => Str("kws.spkSherpa.provider", "cpu");
            set => Put("kws.spkSherpa.provider", value);
        }

        /// <summary>Embedding 的執行緒數。</summary>
        public int SpkSherpaThreads
        {
            get => Int("kws.spkSherpa.threads", 2);
            set => Put("kws.spkSherpa.threads", CoerceIn(value, 1, 8));
        }

        /// <summary>兩次閘門打分之間至少隔這麼久（只有 sherpa 引擎需要；太密的觸發不叫醒）。</summary>
        public int GateMinEvalMs
        {
            get => Int("kws.gateMinEvalMs", 400);
            set => Put("kws.gateMinEvalMs", CoerceIn(value, 0, 10000));
        }

        /// <summary>
        /// 閘門相對 KWS 的**延時**（ms）：KWS 常在名字還沒講完就觸發（實測早約 280 ms），而窗是「結束於判定
        /// 那一刻」的 1.5 s ⇒ 不延時就會切掉名字的尾巴（實測 cos 0.28，完整的名字窗該在 0.5 上下）。
        /// 0＝舊行為（立刻判）。太大＝窗尾會吃進你後面說的話。
        /// </summary>
        public int GateDelayMs
        {
            get => Int("kws.gateDelayMs", 400);
            set => Put("kws.gateDelayMs", CoerceIn(value, 0, 2000));
        }

        /// <summary>閘門判哪一段：`tail`＝觸發那一刻為止的 1.5 s；`best`＝前後 3 s 裡最好的一扇（見 `GateWindow`）。</summary>
        public string GateRule
        {
            get => Str("kws.gateRule", GateWindow.RuleTail);
            set => Put("kws.gateRule", value);
        }

        /// <summary>把閘真的打分的那一扇窗存成 cache/spk_win_*.wav（事後可離線重算）。</summary>
        public bool KeepGateWindow
        {
            get => Flag("kws.keepWindow", false);
            set => Put("kws.keepWindow", value);
        }

        /// <summary>
        /// **收資料用**：把「喚醒網剛剛看過的那一窗」存成 `files/kwswin/` 底下的 wav（每 2 秒最多一個）。
        ///
        /// ⚠️ 開著＝**一直寫檔而且不自動刪**（他會拿走整個目錄）⇒ 收完資料請關。預設關。
        /// </summary>
        public bool DumpWakeWindow
        {
            get => Flag("kws.dumpWindow", false);
            set => Put("kws.dumpWindow", value);
        }

        /// <summary>
        /// How long the floor stays open after a turn, in ms — the pause the owner may take and still be in the
        /// conversation without saying the name again. Renewed by every turn; when it runs out the phone goes
        /// back to answering only to its name. Ten seconds is about one thought.
        /// </summary>
        public int ConversationMs
        {
            get => Int("conversation_ms", 10000);
            set => Put("conversation_ms", CoerceIn(value, 2000, 120000));
        }

        /// <summary>
        /// 「我方連投」的窗口期，ms（owner 2026-10-02）。
        ///
        /// ⚠️ **投出的一律只有佇列裡那幾則**（＝已經畫在畫面上的）；框裡殘留的字**永遠不會被送出去**。
        /// 「還留著字且跟上一次檢查一字不差」只是**判定他停手了**的訊號。
        /// </summary>
        public int OutboxWindowMs
        {
            get => Int("outbox_window_ms", 5000);
            set => Put("outbox_window_ms", CoerceIn(value, 1000, 60000));
        }

        // ---- utterance segmentation (the Python MicListener's parameters, same units) ----
        public int SilenceMs
        {
            get => Int("silence_ms", 700);
            set => Put("silence_ms", value);
        }

        public int RealtimeSilenceMs
        {
            get => Int("realtime_silence_ms", 450);
            set => Put("realtime_silence_ms", value);
        }

        public int MinSpeechMs
        {
            get => Int("min_speech_ms", 300);
            set => Put("min_speech_ms", value);
        }

        public double VadMinRms
        {
            get => Dbl("vad_min_rms", 80.0);
            set => Put("vad_min_rms", value);
        }

        public double VadThresholdFactor
        {
            get => Dbl("vad_threshold_factor", 2.6);
            set => Put("vad_threshold_factor", value);
        }

        /// <summary>Audio kept from before the first voiced block — the front of the first word.</summary>
        public int PrerollMs
        {
            get => Int("preroll_ms", 300);
            set => Put("preroll_ms", CoerceIn(value, 0, 1000));
        }

        // ---- the gates used while the floor is open (see ShellController's floorIsOpen) --------------
        /// <summary>
        /// The VAD multiple in force once the owner has been identified (a wake, or a conversation still live).
        ///
        /// Strict is for the closed door: it has to decide whether the room is talking to itself. Open, the
        /// question is only what he said, and a quieter answer must not be dropped for being quiet.
        /// </summary>
        public double VadRelaxFactor
        {
            get => Dbl("vad_relax_factor", 1.8);
            set => Put("vad_relax_factor", value);
        }

        /// <summary>
        /// Voice-band filtering of the microphone stream (see `VoiceBand`) — before the gates and before
        /// whisper. The cutoffs are in Hz; the defaults keep every speech sound and remove desk rumble.
        /// </summary>
        public bool VoiceFilter
        {
            get => Flag("voice_filter", true);
            set => Put("voice_filter", value);
        }

        public double VoiceHighPass
        {
            get => Dbl("voice_high_pass", VoiceBand.HIGH_PASS_HZ);
            set => Put("voice_high_pass", value);
        }

        public double VoiceLowPass
        {
            get => Dbl("voice_low_pass", VoiceBand.LOW_PASS_HZ);
            set => Put("voice_low_pass", value);
        }

        /// <summary>
        /// Which capture path the microphone opens. The phone's microphone ARRAY is not exposed to an app
        /// directly: the multi-mic denoising sits in the telephony path, so `voice_communication` is the
        /// one that can engage it.
        /// </summary>
        public string AudioSource
        {
            get => Str("audio_source", "voice_recognition");
            set => Put("audio_source", KotlinText.Trim(value));
        }

        /// <summary>Ask for the platform's NoiseSuppressor / AGC / AEC where the device offers them.</summary>
        public bool PlatformEffects
        {
            get => Flag("platform_effects", true);
            set => Put("platform_effects", value);
        }

        public int MaxUtteranceMs
        {
            get => Int("max_utterance_ms", 20000);
            set => Put("max_utterance_ms", value);
        }

        public bool AckEnabled
        {
            get => Flag("ack", true);
            set => Put("ack", value);
        }

        // ---- chat transport ----
        // ⚠️ 2026-10-03 退役：`chatTransport`（鍵 `chat_transport`，"bridge"／"dashboard"）原本是
        // 「這一輪走橋還是走 dashboard」的歷史開關；`GatewayClient` 退役後它恆為 `bridge`，
        // 唯二的讀取點已拆掉 ⇒ 整個鍵移除。手機上殘留的 `chat_transport` 值不再被讀，無害。

        /// <summary>`blueagent-server` base URL, e.g. `http://&lt;nas&gt;:8900`.</summary>
        public string BridgeUrl
        {
            get => Str("bridge_url", "");
            set => Put("bridge_url", KotlinText.Trim(value).TrimEnd('/'));
        }

        /// <summary>Bearer token for the bridge (its `STREAM_TOKEN`; lives in the bridge's .env).</summary>
        public string BridgeToken
        {
            get => Str("bridge_token", "");
            set => Put("bridge_token", value);
        }

        /// <summary>
        /// Which conversation on the bridge this shell talks in.
        ///
        /// ⚠️ **遷移**：iOS 舊版 Swift `Settings` 把這一格存在 `session` 這個鍵名下（2026-10-02 以前）。
        /// 這裡直接讀 `bridge_session`，讀不到才回頭看舊鍵——不然那台手機的會話 id 會被當成「沒設過」
        /// 而回到預設值，等於把他設定頁裡的東西清掉。
        /// </summary>
        public string BridgeSession
        {
            get
            {
                var v = Str("bridge_session", "");
                if (v.Length > 0) return v;
                if (_backend.Contains("session")) return Str("session", DefaultSession);
                return DefaultSession;
            }
            set => Put("bridge_session", KotlinText.Trim(value));
        }

        /// <summary>
        /// 開機時把這一格的**唯一性**定下來（owner 2026-10-03：「所有機器預設第一個 session 都是
        /// `blueagent-phone`，會直接撞機器，應該一開始就給隨機 id」＋「不要前綴，直接用 UUID」）。
        ///
        /// 出貨預設名只當**種子**，落地的是**純 UUID**：規則在 `SessionIds.EnsureUnique` ——
        /// 空白／出貨預設名／只剩舊前綴 ⇒ 換 UUID；已經唯一的 ⇒ 原樣（不重 roll，免得把他那台裝置在橋側的
        /// 會話記憶切掉）。只在真的變了才寫檔。
        /// </summary>
        public string EnsureSessionId()
        {
            // Kotlin 的無參版＝ `ensureSessionId(Random.Default)`；C# 這一側走
            // `SessionIds.EnsureUnique(current)`（同一個系統亂數來源、同一條規則）。
            var next = SessionIds.EnsureUnique(BridgeSession);
            if (next != BridgeSession) BridgeSession = next;
            return next;
        }

        /// <summary>
        /// 同上，但可注入 <paramref name="random"/>（單測固定序列用）。
        ///
        /// ⚠️ Kotlin 那份刻意**不做成預設參數**（預設參數不會過 framework 頭檔，iOS 那側就編不出
        /// 無參版本）；C# 沒有那個毛病，所以兩個 overload 都給——**行為必須一樣**。
        /// </summary>
        public string EnsureSessionId(IRandomSource random)
        {
            var next = SessionIds.EnsureUnique(BridgeSession, random);
            if (next != BridgeSession) BridgeSession = next;
            return next;
        }

        /// <summary>
        /// True once the user has put in the bridge URL.
        ///
        /// ⚠️ 2026-10-03：以前是「gateway 或橋填了就算」，但**直連 gateway 那條線已退役** ⇒ 只認橋。
        /// </summary>
        public bool Configured => BridgeUrl.Length > 0;

        /// <summary>預設名只有**一個**來源（`SessionIds.DEFAULT`）。</summary>
        public const string DefaultSession = SessionIds.Default;

        /// <summary>`Int.coerceIn(min, max)`（Kotlin 在 min &gt; max 時丟例外；這裡不需要那條路）。</summary>
        private static int CoerceIn(int v, int lo, int hi) => v < lo ? lo : (v > hi ? hi : v);
    }
}
