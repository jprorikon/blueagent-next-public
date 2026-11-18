using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using BlueAgent.Logic;
using BlueAgent.Logic.Motion;
using BlueAgent.Next;
using BlueAgent.Spike;
using UnityEngine;

namespace BlueAgent.MR
{
    /// <summary>
    /// MR 端接**同一條橋**（PLAN-mr-quest3 M3；§4-2：不新增協議、服務端一行不改）。
    ///
    /// 用的就是手機端那一支 `NextApi`（`/api/me`、`/api/audio` 的 SSE、`/api/events` 常駐推播、動作片段），
    /// 但帶 `X-Source: mr`：這一端說的話不進手機的聊天紀錄、推播只回 MR、通話是自己的一通（後撥的蓋掉先撥的）。
    /// 這裡只做 MR 需要的那一小段「一輪」的**演出**：聽到什麼 → 她的字 → 她的聲音（排隊播）→ 動作 → 結束。
    /// 送語音、斷句、打斷是通話那一支（<see cref="MrCall"/>）的事（owner 2026-11-01：手勢單句模式下線）。
    /// 換人：<see cref="SetEntity"/>（`/api/me` 的 `entities[]` 存成清單給左手雙擊輪換）。
    ///
    /// **登入**（owner 2026-11-01）：頭顯裡打不了字 ⇒ owner 在電腦上把 token 寫進檔案、`adb push` 到
    /// 這個 app 的私有目錄（<see cref="TokenFileName"/>）；啟動時讀到就存進 PlayerPrefs、**立刻刪檔**。
    /// 要出聲得是 master 的 token（伺服器只讓 master 的 `want_voice` 生效）。token 一律不進 log。
    /// </summary>
    public sealed class MrBridge : MonoBehaviour
    {
        /// <summary>`Application.persistentDataPath` 底下的檔名（Quest 上＝`/sdcard/Android/data/&lt;包名&gt;/files/`）。</summary>
        public const string TokenFileName = "bridge-token";
        /// <summary>連不上 `/api/me` 時多久重試（秒）。</summary>
        private const float RetrySeconds = 10f;
        /// <summary>
        /// 沒有 token 時多久看一次有沒有人 push 了 token 檔（秒）：push 完不必重開 app（評審 2026-11-01 P2-5）。
        /// </summary>
        private const float TokenPollSeconds = 5f;
        private float _tokenPollAt = -1f;

        public NextApi Api { get; private set; }
        public SpeechPlayer Voice { get; private set; }

        /// <summary>現在的連線狀態（已經換成這一種語言的字；儀表第一行）。</summary>
        public string Status { get; private set; } = "";
        public bool Ready { get; private set; }
        /// <summary>
        /// 一輪送出去、還沒收到 `done`（她在想／在說；氣泡與空閒判斷看它）。只是狀態，**不是門**：
        /// 一次一句由 <see cref="MrCall"/> 自己管（WO §3：通話不走這道門）。
        /// </summary>
        public bool Busy { get; private set; }
        /// <summary>收到了思考類的幀、還沒開始說（`thinking`／`memory`／`tool`／`accepted`）。</summary>
        public bool Thinking { get; private set; }
        /// <summary>她正在出聲（排隊中的也算）。</summary>
        public bool Speaking => _speech.Count > 0 || (Voice != null && Voice.IsPlaying);
        /// <summary>這一輪伺服器聽到你說了什麼。</summary>
        public string Heard { get; private set; } = "";
        /// <summary>她這一輪（或推播）說的字。</summary>
        public string Said { get; private set; } = "";
        /// <summary>動作片段來了（MrShell 拿去給 3D 角色播）。</summary>
        public event Action<MotionClip> Motion;
        /// <summary>
        /// 該讓你知道的連線事件（連上、斷了、token 不對、這一輪失敗；已經是這一種語言的字）⇒ 視野下方的通知。
        /// 同一句不連發（斷線每 10 秒重試一次，不要每次都跳）。
        /// </summary>
        public event Action<string> Notice;
        private string _noticed;

        /// <summary>現在對話的角色 id（換人＝<see cref="SetEntity"/>）。</summary>
        public string Entity => _entity;
        /// <summary>她的名字（`/api/me` 給的顯示名；儀表第一行）。</summary>
        public string Name => _name;
        /// <summary>是 master 的 token（通話 `X-Call` 只認 master；guest 撥會 403）。</summary>
        public bool Master => _master;

        private string _entity;
        private string _name;
        private bool _master;
        /// <summary>`/api/me` 的 `entities[]`（id → 名字，照伺服器給的順序）：左手雙擊輪換用。</summary>
        private readonly List<KeyValuePair<string, string>> _entities = new List<KeyValuePair<string, string>>();
        private float _retryAt = -1f;
        private readonly Queue<string> _speech = new Queue<string>();
        private Coroutine _pump;
        private readonly Dictionary<string, MotionClip> _motions = new Dictionary<string, MotionClip>();

        public static MrBridge Create(string entity)
        {
            var go = new GameObject("MR Bridge");
            go.SetActive(false);
            var bridge = go.AddComponent<MrBridge>();
            bridge._entity = entity;
            bridge._name = entity;
            bridge.Api = go.AddComponent<NextApi>();
            // 自己一條管道（owner 2026-11-01）：紀錄、推播、通話都跟手機分開（伺服器看 `X-Source`）
            bridge.Api.Source = "mr";
            bridge.Voice = go.AddComponent<SpeechPlayer>();
            go.SetActive(true);
            return bridge;
        }

        private void Start()
        {
            Api.BaseUrl = NextConfig.BaseUrl;
            Api.Unauthorized += OnUnauthorized;
            AdoptTokenFile();
            Connect();
        }

        /// <summary>有 push 進來的 token 檔就收下（只認 `ba_`／`ot_`／`gt_` 開頭的一行）、存起來、刪檔。</summary>
        private static void AdoptTokenFile()
        {
            var path = Path.Combine(Application.persistentDataPath, TokenFileName);
            if (!File.Exists(path)) return;
            try
            {
                var token = File.ReadAllText(path).Trim();
                File.Delete(path);
                if (token.StartsWith("ba_") || token.StartsWith("ot_") || token.StartsWith("gt_"))
                {
                    NextConfig.Token = token;
                    Debug.Log($"[mr] 橋：收下 push 進來的 token（{token.Length} 字元，已刪檔）");
                }
                else
                {
                    Debug.LogWarning("[mr] 橋：token 檔格式不對（不是 ba_／ot_／gt_ 開頭），已刪檔、不採用");
                }
            }
            catch (Exception error)
            {
                Debug.LogWarning("[mr] 橋：讀 token 檔失敗：" + error.Message);
            }
        }

        private void Connect()
        {
            _retryAt = -1f;
            var token = NextConfig.Token;
            if (string.IsNullOrEmpty(token))
            {
                Status = NextStrings.F("mr_bridge_no_token", "path", Path.Combine(Application.persistentDataPath, TokenFileName));
                Tell(Status);
                _tokenPollAt = Time.unscaledTime + TokenPollSeconds;
                return;
            }
            _tokenPollAt = -1f;
            Api.Token = token;
            Status = NextStrings.Get("mr_bridge_connecting");
            Api.Get("/api/me", reply =>
            {
                if (!reply.Ok)
                {
                    if (reply.Status == 401) return;                // OnUnauthorized 收口
                    Status = NextStrings.F("mr_bridge_offline", "reason", reply.UserMessage());
                    Tell(Status);
                    Debug.LogWarning($"[mr] 橋：/api/me 失敗（{reply.Describe()}）⇒ {RetrySeconds:F0} 秒後重試");
                    _retryAt = Time.unscaledTime + RetrySeconds;
                    return;
                }
                var me = reply.Json;
                _master = me.OptString("role") == "master";
                var entities = me.OptJArray("entities");
                _entities.Clear();
                for (var i = 0; entities != null && i < entities.Length(); i++)
                {
                    var entry = entities.OptJObject(i);
                    var id = entry != null ? entry.OptString("id") : "";
                    if (string.IsNullOrEmpty(id)) continue;
                    _entities.Add(new KeyValuePair<string, string>(id, entry.OptString("name", id)));
                }
                _name = NameOf(_entity);
                Ready = true;
                Status = NextStrings.Get(_master ? "mr_bridge_ready" : "mr_bridge_ready_guest");
                Tell(Status);
                Debug.Log($"[mr] 橋：連上（role={me.OptString("role")}，她＝{_name}）");
                Api.StartEvents(OnEvent, OnEventsState);
            });
        }

        private void Update()
        {
            if (_retryAt > 0f && Time.unscaledTime >= _retryAt) Connect();
            if (_tokenPollAt > 0f && Time.unscaledTime >= _tokenPollAt)
            {
                AdoptTokenFile();
                if (!string.IsNullOrEmpty(NextConfig.Token)) Connect();
                else _tokenPollAt = Time.unscaledTime + TokenPollSeconds;
            }
        }

        private void OnUnauthorized()
        {
            Ready = false;
            NextConfig.Token = "";
            Api.StopEvents();
            Status = NextStrings.Get("mr_bridge_unauthorized");
            Tell(Status);
            _tokenPollAt = Time.unscaledTime + TokenPollSeconds;      // 等新的 token 檔
            Debug.LogWarning("[mr] 橋：401 ⇒ token 無效或被撤銷，已清掉");
        }

        /// <summary>連線那幾句：跟上一句一樣就不再跳（斷線重試、重連成功才各說一次）。</summary>
        private void Tell(string text)
        {
            if (text == _noticed) return;
            _noticed = text;
            Notice?.Invoke(text);
        }

        private void OnEventsState(bool connected, string text)
        {
            if (text == "unauthorized") return;
            Debug.Log($"[mr] 橋：推播 {(connected ? "連上" : "斷線（" + text + "）")}");
        }

        /// <summary>
        /// 輪換的下一個（`/api/me` 的順序循環；只算有 3D 模型的）。只有她一個（或還沒連上）⇒ null。
        /// </summary>
        public string NextEntity()
        {
            var count = _entities.Count;
            var at = _entities.FindIndex(pair => pair.Key == _entity);
            // 走滿一圈（step＝count 回到自己，被下面那條跳過）；自己不在清單裡（at＝-1）時也看得到最後一位
            for (var step = 1; step <= count; step++)
            {
                var id = _entities[(at + step + count) % count].Key;
                if (id != _entity && NextStage3D.Has(id)) return id;
            }
            return null;
        }

        /// <summary>
        /// 換人（WO §4-1）：她的聲音停掉、在途的一輪取消、字清掉；之後的 `/api/audio`、推播過濾、動作都跟新的那一位。
        /// 通話要先掛（一通只對一個角色）——那是 <see cref="MrShell"/> 的事，這裡不管。
        /// </summary>
        public void SetEntity(string id)
        {
            if (string.IsNullOrEmpty(id) || id == _entity) return;
            StopSpeech();
            Api.CancelTurn();
            EndTurn();
            Heard = "";
            Said = "";
            _entity = id;
            _name = NameOf(id);
            Debug.Log($"[mr] 橋：換人 ⇒ {id}（{_name}）");
        }

        private string NameOf(string id)
        {
            foreach (var pair in _entities)
            {
                if (pair.Key == id) return pair.Value;
            }
            return id;
        }

        /// <summary>通話送出一句：清掉上一句的字、進「在想」（她的聲音由 <see cref="MrCall"/> 先停）。</summary>
        public void BeginTurn()
        {
            Busy = true;
            Thinking = true;
            Heard = "";
            Said = "";
        }

        /// <summary>這一輪結束（收完、被打斷、送不成）：只收狀態，要說什麼由呼叫端決定。</summary>
        public void EndTurn()
        {
            Busy = false;
            Thinking = false;
        }

        /// <summary>你開始說話時：停掉她現在的聲音（不然錄進去的都是她）。</summary>
        public void StopSpeech()
        {
            _speech.Clear();
            if (_pump != null) StopCoroutine(_pump);
            _pump = null;
            if (Voice != null) Voice.Stop();
        }

        /// <summary>一輪的 SSE 幀 ⇒ 字、聲音、動作（通話那一支過濾掉過期的票之後轉進來）。</summary>
        public void Present(string name, JObject data)
        {
            var payload = data.OptJObject("payload");
            switch (name)
            {
                case "transcript":
                    Heard = payload != null ? payload.OptString("text") : data.OptString("text");
                    Status = NextStrings.Get("mr_thinking");
                    break;
                case "accepted":
                case "thinking":
                case "memory":
                case "tool":
                    Thinking = true;
                    Status = NextStrings.Get("mr_thinking");
                    break;
                case "part":
                    // server 切好的段落（跟 TTS 同一個句柄）：顯示最新那一段就好
                    if (payload != null) Said = payload.OptString("text");
                    Thinking = false;
                    break;
                case "delta":
                    if (payload != null) Said += payload.OptString("text");
                    Thinking = false;
                    break;
                case "speech":
                    if (payload != null) Enqueue(payload.OptString("audio"));
                    Thinking = false;
                    break;
                case "speech_error":
                    Debug.LogWarning("[mr] 橋：這一句沒有聲音（" + (payload != null ? payload.OptString("reason") : "") + "）");
                    break;
                case "motion":
                    FetchMotion(payload);
                    break;
                case "done":
                    if (payload != null && !string.IsNullOrEmpty(payload.OptString("text"))) Said = payload.OptString("text");
                    Busy = false;
                    Thinking = false;
                    Status = NextStrings.Get(_master ? "mr_bridge_ready" : "mr_bridge_ready_guest");
                    break;
                case "error":
                    Busy = false;
                    Thinking = false;
                    Status = NextStrings.F("mr_turn_failed", "reason", NextStrings.ErrorCode(NextText.StreamErrorCode(data)));
                    Notice?.Invoke(Status);                     // 每一輪的失敗都要說（不跟連線那幾句去重）
                    break;
            }
        }

        /// <summary>
        /// 常駐 SSE 的事件分流（2026-11-04）：`NextApi.StartEvents` 現在連事件名一起給
        /// ⇒ `mood` 是手機徽章的狀態事件，MR 不畫徽章、也**不能**當成她說的一句話。
        /// </summary>
        private void OnEvent(string eventName, JObject payload)
        {
            if (eventName != "delivery") return;
            OnDelivery(payload);
        }

        /// <summary>推播（語音回合「先短回、後交付」的那段完整答案、她主動開口）：字＋聲音照樣上。</summary>
        private void OnDelivery(JObject delivery)
        {
            var from = delivery.OptString("entity");
            if (!string.IsNullOrEmpty(from) && from != _entity) return;
            // 只帶聲音（或什麼都沒帶）的推播不要把上一句的字洗掉（2026-11-01 實機：推播 0 字，儀表上她的話就不見了）
            var text = delivery.OptString("text");
            if (!string.IsNullOrEmpty(text)) Said = text;
            Enqueue(delivery.OptString("audio"));
            Debug.Log($"[mr] 橋：推播一句（{Said.Length} 字{(string.IsNullOrEmpty(delivery.OptString("audio")) ? "" : "＋聲音")}）");
        }

        /// <summary>語音會來不只一段（短應答＋完整答案）⇒ 排隊，一段播完才播下一段（手機端同一個作法）。</summary>
        private void Enqueue(string base64)
        {
            if (string.IsNullOrEmpty(base64)) return;
            _speech.Enqueue(base64);
            if (_pump == null) _pump = StartCoroutine(PumpSpeech());
        }

        private IEnumerator PumpSpeech()
        {
            while (_speech.Count > 0)
            {
                var clip = _speech.Dequeue();
                try { Voice.PlayBase64(clip, "mr-speech"); }
                catch (Exception error) { Debug.LogWarning("[mr] 播放失敗：" + error.Message); }
                // 診斷（2026-11-01 實機「聽不到她說話」）：播的時候實際流過 AudioSource 的峰值。
                // 峰值 ≈ 0 ⇒ 資料本身是靜音；峰值正常 ⇒ 資料有出去，問題在輸出／音量
                var peak = 0.0;
                while (Voice != null && Voice.IsPlaying)
                {
                    peak = Math.Max(peak, Voice.LastPeak);
                    yield return null;
                }
                Debug.Log($"[mr] 播完一句：峰值 {peak:F3}、音量 {(Voice != null ? Voice.Volume : 0f):F2}、"
                          + $"聽者音量 {AudioListener.volume:F2}{(AudioListener.pause ? "（聽者暫停中！）" : "")}");
                yield return null;
            }
            _pump = null;
        }

        /// <summary>`motion` 幀 ⇒ 抓片段（`bamotion/1`；抓過的記著）⇒ 交給 MrShell 播。</summary>
        private void FetchMotion(JObject payload)
        {
            if (payload == null) return;
            var entity = payload.OptString("entity");
            if (!string.IsNullOrEmpty(entity) && entity != _entity) return;
            var id = payload.OptString("id");
            var url = payload.OptString("url");
            if (string.IsNullOrEmpty(url)) return;
            if (!string.IsNullOrEmpty(id) && _motions.TryGetValue(id, out var known))
            {
                Motion?.Invoke(known);
                return;
            }
            Api.GetBytes(url, bytes =>
            {
                if (bytes == null) return;
                MotionClip clip;
                try
                {
                    clip = MotionClip.Parse(System.Text.Encoding.UTF8.GetString(bytes));
                }
                catch (Exception error)
                {
                    Debug.LogWarning("[mr] 動作片段解不開：" + error.Message);
                    return;
                }
                if (!string.IsNullOrEmpty(clip.Id)) _motions[clip.Id] = clip;
                Debug.Log($"[mr] 動作：{clip.Tag}（{clip.Id}，{clip.Seconds:F1} s）");
                Motion?.Invoke(clip);
            });
        }

        private void OnDestroy()
        {
            if (Api != null)
            {
                Api.Unauthorized -= OnUnauthorized;
                Api.StopEvents();
            }
        }
    }
}
