using BlueAgent.Screens;
using System;
using System.Collections;
using System.Collections.Generic;
using System.Text;
using BlueAgent.Logic;
using BlueAgent.Spike;
using UnityEngine;
using UnityEngine.Networking;

namespace BlueAgent.Next
{
    /// <summary>
    /// blueagent-next 的 HTTP 客戶端（端末唯一一條連線）。
    ///
    /// 契約：倉庫根目录 `docs/APP-API-SSE.md`（v1＋v2 補拿）與 `docs/accounts-and-clients.md`。
    /// **只做契約裡有的形狀**：一般請求回 JSON；`/api/turn`、`/api/events` 是 SSE。
    ///
    /// SSE 的切幀規則不寫在這裡——用共用邏輯層的 `SseStream`（與舊橋同一份）＋現成的
    /// `SseDownloadHandler`。這裡只負責：帶 token、送 body、把 (event, data) 往上交。
    /// </summary>
    public sealed class NextApi : MonoBehaviour
    {
        /// <summary>伺服器根位址（不含結尾斜線）。</summary>
        public string BaseUrl = NextConfig.DefaultBaseUrl;

        /// <summary>`Authorization: Bearer &lt;token&gt;`。</summary>
        public string Token = "";
        /// <summary>
        /// 哪一端（每個請求的 `X-Source`）：手機＝`phone`、Quest 3 的 MR＝`mr`（owner 2026-11-01：各走各的管道）。
        /// 伺服器拿它分：這一輪的來源標籤（`mr:call`）、推播送回哪一端、通話是哪一端撥的（後撥的蓋掉先撥的）、
        /// 有沒有舞台（`motion` 幀）。
        /// </summary>
        public string Source = "phone";

        /// <summary>一次一般請求的結果（JSON 或錯誤）。</summary>
        public sealed class Reply
        {
            public int Status;
            public string Body = "";
            public string Error = "";
            public JObject Json;
            /// <summary>伺服器給的雜湊（`ETag`，去掉引號）；有快取的那幾條才有。</summary>
            public string Etag = "";

            public bool Ok { get { return Status >= 200 && Status < 300 && Json != null; } }

            /// <summary>帶了 `If-None-Match` 而線上沒變（304）：用本機那份。</summary>
            public bool NotModified { get { return Status == 304; } }

            /// <summary>契約 §4 的錯誤碼（`unauthorized`／`forbidden`／…）；沒有就用 HTTP 狀態。</summary>
            public string Code
            {
                get
                {
                    if (Json != null)
                    {
                        var code = Json.OptString("error");
                        if (!string.IsNullOrEmpty(code)) return code;
                    }
                    if (Status > 0) return "http_" + Status;
                    return string.IsNullOrEmpty(Error) ? "unknown" : Error;
                }
            }

            public string Describe()
            {
                if (Status == 0) return "offline (" + Error + ")";
                if (Json != null)
                {
                    var code = Json.OptString("error");
                    if (!string.IsNullOrEmpty(code)) return Status + " " + code;
                }
                var head = Body == null ? "" : Body.Trim();
                if (head.Length > 120) head = head.Substring(0, 120);
                return Status + (string.IsNullOrEmpty(head) ? "" : " " + head);
            }

            /// <summary>
            /// 給人看的一句話（界面語言）：伺服器錯誤碼有專屬說法就用它，其次照 HTTP 狀態（`NextText.ErrorKey`）。
            /// 端末不要自己發明語意：表裡只翻譯契約裡已有的那幾種，其餘落到通用的「エラー（狀態 碼）」。
            /// </summary>
            public string UserMessage()
            {
                return NextStrings.Error(Status, Json != null ? Json.OptString("error") : "");
            }
        }

        /// <summary>常駐推播那條連線（同樣只由自己的協程清，見 `_turnRequest`）。</summary>
        private readonly OwnedSlot<UnityWebRequest> _eventsRequest = new OwnedSlot<UnityWebRequest>();
        /// <summary>平台实时通话的独立 SSE；MR 仍使用旧 /api/voice 控制路径。</summary>
        private readonly OwnedSlot<UnityWebRequest> _callEventsRequest = new OwnedSlot<UnityWebRequest>();
        /// <summary>
        /// 契約 v2：重連要帶 `Last-Event-ID`，斷線那幾條才補得回來；跨重啟也要記得。
        /// **按「伺服器＋身份」分區**（unity-review U-1）：殼在 `/api/me` 確定是誰之後 `Bind`，登出 `Unbind`。
        /// </summary>
        public readonly EventCursors Cursors = new EventCursors(new NextPrefs());
        /// <summary>登入期代次：回呼出發時記下，回來時換了一段 ⇒ 丟掉（U-3）；401 也在這裡收口（U-6）。</summary>
        public readonly SessionGate Session = new SessionGate();
        /// <summary>
        /// App 側快取（owner 2026-10-29：圖片、表情包、聊天記錄不要每次重抓，對比線上雜湊再決定）。
        /// 跟事件游標一樣**按「伺服器＋身份」分**：殼在 `/api/me` 確定是誰之後 <see cref="BindCache"/>，登出拿掉。
        /// 沒綁（還沒登入）⇒ null，一切照舊走網路。
        /// </summary>
        public SyncCache Cache { get; private set; }
        /// <summary>S2 持久社交副本；跟 Cache 一樣按伺服器＋身份綁定，但放 persistentDataPath，不由系統清缓存。</summary>
        public SocialRepository Social { get; private set; }
        private NextSocialSync _socialSync;
        public NextSocialSync SocialSync { get { return _socialSync ?? (_socialSync = new NextSocialSync(this)); } }
        /// <summary>S3 聊天完整副本與持久 Outbox；所有聊天回包和事件先落這裡。</summary>
        public ChatRepository Chat { get; private set; }
        /// <summary>App Server 會話使用的 canonical account id；不要拿 legacy principal 計算 direct id。</summary>
        public string ChatAccountId { get; private set; } = "";

        /// <summary>
        /// 本機資料庫這次開不起來的原文（空＝正常）。2026-11-07 Android 實機：`DllImport("sqlite3")`
        /// 在 Android 沒有對應的 `.so` ⇒ 登入後建 social.db／chat.db 直接拋 DllNotFoundException，
        /// 協程死在 `Enter()` 裡 ⇒ 畫面停在「接續中」＋空白。現在這裡吞下例外但**原文留著**，
        /// 殼會把它照原樣顯示出來（不用兜底話掩蓋），並且至少讓登入流程走完。
        /// </summary>
        public string CacheError { get; private set; } = "";
        private NextChatSync _chatSync;
        public NextChatSync ChatSync { get { return _chatSync ?? (_chatSync = new NextChatSync(this)); } }
        /// <summary>登入中的任何請求收到 401 ⇒ 叫一次（同一段登入只一次）；殼接去清 token 回登入頁。</summary>
        public event Action Unauthorized;
        private Coroutine _eventsRoutine;
        /// <summary>在途的那一輪（打字／語音／圖片）：只有自己的協程收尾時才清得掉（U-2）。</summary>
        private readonly OwnedSlot<UnityWebRequest> _turnRequest = new OwnedSlot<UnityWebRequest>();
        /// <summary>在途的位置上報：登出停止迴圈時也要 Abort 已發出的那一發（U-3）。</summary>
        private readonly OwnedSlot<UnityWebRequest> _locationRequest = new OwnedSlot<UnityWebRequest>();
        /// <summary>可同時選多張照片，所以各自跟蹤；登出時全部 Abort，不用單槽互相取消。</summary>
        private readonly List<UnityWebRequest> _mediaRequests = new List<UnityWebRequest>();

        /// <summary>常駐推播那條線現在通不通（狀態列用）：`off`／`connecting`／`online`／`reconnecting`／`unauthorized`。</summary>
        public string EventsState { get; private set; } = "off";

        /// <summary>斷線重連次數（診斷頁用）。</summary>
        public int Reconnects { get; private set; }

        /// <summary>
        /// 有幾幀的處理器拋過例外（游標會停在那一幀、重連時由服務端重放）。
        /// 這個數字不該一直長：長了代表某一類幀一直處理不了（診斷頁用）。
        /// </summary>
        public int FrameErrors { get; private set; }

        // ── App 側快取 ───────────────────────────────────────────────────────────

        /// <summary>
        /// 換成這個人的快取（空 principal ⇒ 拿掉）。聊天與社交資料放 persistentDataPath，
        /// 各自按「伺服器＋身份」隔離；換帳號先作廢舊 worker，避免晚到回包寫進新資料庫。
        /// </summary>
        public void BindCache(string principal, string chatAccountId = "")
        {
            // BA_SELFTEST の transport override がストレージ identity を変えないよう canonical URL を使う。
            // 本番では BaseUrl == DefaultBaseUrl なので、既存のキャッシュ／Outbox 配置は変わらない。
            var folder = SyncCache.FolderFor(NextConfig.DefaultBaseUrl, principal);
            ChatAccountId = string.IsNullOrEmpty(chatAccountId) ? (principal ?? "") : chatAccountId;
            CacheError = "";
            _socialSync?.Cancel();
            _chatSync?.Cancel();
            Social?.Dispose();
            Chat?.Dispose();
            Social = null;
            Chat = null;
            Cache = string.IsNullOrEmpty(folder)
                ? null
                : new SyncCache(new SystemFileSystem(),
                                System.IO.Path.Combine(Application.temporaryCachePath, "next-sync", folder),
                                new SystemClock());
            if (!string.IsNullOrEmpty(folder))
            {
                var root = System.IO.Path.Combine(Application.persistentDataPath, "next-social", folder);
                // 2026-11-07 實機坑：Android 沒有 libsqlite3（iOS／macOS 有系統的）⇒ 這裡第一次建庫就拋
                // DllNotFoundException，而它一路炸回 `NextShell.Enter()` 的協程 ⇒ 頂欄停在「接續中」、
                // 身體全空。原生庫已隨 APK 帶上（Assets/Plugins/Android/libs/arm64-v8a/libsqlite3.so），
                // 但任何裝置上開庫失敗都不該吃掉整頁：例外接住、原文留給殼顯示，這次就走純網路。
                try
                {
                    Social = new SocialRepository(System.IO.Path.Combine(root, "social.db"));
                    Chat = new ChatRepository(System.IO.Path.Combine(root, "chat.db"));
                    SocialSync.Kick();
                    ChatSync.Kick();
                }
                catch (Exception error)
                {
                    Social?.Dispose();
                    Chat?.Dispose();
                    Social = null;
                    Chat = null;
                    CacheError = error.GetType().Name + ": " + error.Message;
                    UnityEngine.Debug.LogError("[next] 本機資料庫開不起來（這次走純網路）：" + CacheError);
                }
            }
        }

        /// <summary>本機存的那份 JSON（聊天記錄、表情包清單）；沒有／壞了 ⇒ null。</summary>
        public JObject CachedJson(string key)
        {
            var text = Cache?.GetText(key);
            if (string.IsNullOrEmpty(text)) return null;
            try { return new JObject(text); }
            catch (Exception) { return null; }
        }

        /// <summary>
        /// 200 回來的 JSON 連同雜湊存起來（下次帶著雜湊來問）。
        /// 雜湊優先用 `ETag`；回應裡沒有（被代理丟了）就用本文裡同一個值的欄位
        /// （<paramref name="tagField"/>：聊天記錄是 `digest`、表情包清單是 `version`）。
        /// </summary>
        public void StoreJson(string key, Reply reply, string tagField)
        {
            if (Cache == null || reply == null || !reply.Ok) return;
            var tag = !string.IsNullOrEmpty(reply.Etag) ? reply.Etag : reply.Json.OptString(tagField);
            if (string.IsNullOrEmpty(tag)) return;               // 舊伺服器：沒有雜湊就不存（存了也問不出 304）
            Cache.PutText(key, reply.Body, tag);
        }

        /// <summary>
        /// 本機有、而且雜湊跟線上給的一樣 ⇒ 直接給（不發請求）；<paramref name="hash"/> 空字串＝線上沒說，
        /// 那就有就給（表情包的圖幾乎不會換；真的換了，打開表情盤拿到新清單時會按雜湊重抓）。
        /// </summary>
        public byte[] CachedBytes(string path, string hash)
        {
            if (Cache == null || string.IsNullOrEmpty(path)) return null;
            if (!string.IsNullOrEmpty(hash) && !Cache.Fresh(path, hash)) return null;
            return Cache.Get(path);
        }

        // ── 一般請求 ─────────────────────────────────────────────────────────────

        public void Get(string path, Action<Reply> done)
        {
            StartCoroutine(Routine(path, UnityWebRequest.kHttpVerbGET, null, 20, done));
        }

        /// <summary>帶 `If-None-Match` 的 GET：線上沒變 ⇒ <see cref="Reply.NotModified"/>（沒有本文）。</summary>
        public void GetIfChanged(string path, string etag, Action<Reply> done)
        {
            StartCoroutine(Routine(path, UnityWebRequest.kHttpVerbGET, null, 20, done, null, etag));
        }

        public void Post(string path, JObject body, Action<Reply> done)
        {
            StartCoroutine(Routine(path, UnityWebRequest.kHttpVerbPOST, body, 30, done));
        }

        public void Put(string path, JObject body, Action<Reply> done)
        {
            StartCoroutine(Routine(path, UnityWebRequest.kHttpVerbPUT, body, 30, done));
        }

        public void Delete(string path, Action<Reply> done)
        {
            StartCoroutine(Routine(path, UnityWebRequest.kHttpVerbDELETE, null, 30, done));
        }

        /// <summary>
        /// 契約 v2：`GET /api/turns?since=&lt;last_good&gt;` 把斷線期間漏掉的回合補回來。
        /// `entity` 一定要帶：這條線是按 **principal** 發的，不帶就等於把同一主人所有角色的回合都補進來
        /// （切角色後畫面混進另一個人的話）。
        /// `source`＝來源端前綴（`phone`）：App 與 Discord 的紀錄各自獨立（owner 2026-10-27）。
        /// </summary>
        public void GetTurnsSince(string since, string entity, string source, Action<Reply> done)
        {
            var path = "/api/turns?limit=50";
            if (!string.IsNullOrEmpty(since)) path += "&since=" + UnityWebRequest.EscapeURL(since);
            if (!string.IsNullOrEmpty(entity)) path += "&entity=" + UnityWebRequest.EscapeURL(entity);
            if (!string.IsNullOrEmpty(source)) path += "&source=" + UnityWebRequest.EscapeURL(source);
            Get(path, done);
        }

        /// <summary>
        /// `GET /api/turns?entity=&lt;id&gt;&amp;limit=N[&amp;before=&lt;turn_id&gt;]`：**按角色**取历史（P1）。
        /// 旧壳是本地 `history.jsonl`；新仓以服务端为源（跨端一致、不必自己维护落盘）。
        /// `before` 用来往前翻页（第二页的最后一条＝下一页的 `before`）。
        /// </summary>
        public void GetTurns(string entity, string before, int limit, string source, Action<Reply> done,
                             string etag = "")
        {
            var path = "/api/turns?limit=" + (limit <= 0 ? 50 : limit);
            if (!string.IsNullOrEmpty(entity)) path += "&entity=" + UnityWebRequest.EscapeURL(entity);
            if (!string.IsNullOrEmpty(before)) path += "&before=" + UnityWebRequest.EscapeURL(before);
            if (!string.IsNullOrEmpty(source)) path += "&source=" + UnityWebRequest.EscapeURL(source);
            GetIfChanged(path, etag, done);
        }

        /// <summary>
        /// `POST /api/image`（P4）：raw 圖檔 → SSE（第一幀 `image`，之後與打字同一條）。
        /// `note` 走 header `X-Note`，**必須 EscapeURL**（header 是 latin-1，日文/中文直接塞會亂碼）。
        /// `wantVoice` 走 header `X-Want-Voice`：**圖片那條從前沒帶** ⇒ 服務端不出聲，
        /// 看起來就是「發圖之後她不講話」（2026-10-27 owner 實測）。跟文字／語音兩條對齊。
        /// </summary>
        public Coroutine SendImage(byte[] image, string entity, string note, bool wantVoice,
                                   Action<string, JObject> onFrame, Action<string> onClosed)
        {
            CancelTurn();
            return StartCoroutine(ImageRoutine(image, entity, note, wantVoice, onFrame, onClosed));
        }

        private IEnumerator ImageRoutine(byte[] image, string entity, string note, bool wantVoice,
                                         Action<string, JObject> onFrame, Action<string> onClosed)
        {
            var epoch = Session.Epoch;
            var failure = "";
            var status = 0;
            using (var request = new UnityWebRequest(BaseUrl + "/api/image", UnityWebRequest.kHttpVerbPOST))
            {
                _turnRequest.Set(request);
                try
                {
                    request.uploadHandler = new UploadHandlerRaw(image);
                    request.downloadHandler = new SseDownloadHandler(wires =>
                    {
                        if (Session.IsCurrent(epoch)) Dispatch(wires, onFrame);
                    });
                    request.SetRequestHeader("Authorization", "Bearer " + Token);
                    request.SetRequestHeader("X-Source", Source);
                    request.SetRequestHeader("Accept", "text/event-stream");
                    request.SetRequestHeader("X-Audio-Profile", "mulaw-v1");
                    request.SetRequestHeader("Content-Type", ImageType(image));
                    if (!string.IsNullOrEmpty(entity)) request.SetRequestHeader("X-Entity", entity);
                    if (wantVoice) request.SetRequestHeader("X-Want-Voice", "1");
                    if (!string.IsNullOrEmpty(note)) request.SetRequestHeader("X-Note", UnityWebRequest.EscapeURL(note));
                    request.timeout = 300;
                    yield return request.SendWebRequest();
                    status = (int)request.responseCode;
                    if (request.responseCode >= 400)
                    {
                        var detail = (request.downloadHandler as SseDownloadHandler)?.Text;
                        var probe = new Reply { Status = (int)request.responseCode,
                                               Error = string.IsNullOrEmpty(detail) ? request.error : detail };
                        failure = probe.UserMessage();
                    }
                }
                finally
                {
                    // 中途丟例外也要清：不然槽裡留著已 Dispose 的請求，下一次取消就對它 Abort 而炸掉（複檢 2026-10-28）
                    _turnRequest.ClearIf(request);
                }
            }
            if (!Settle(status, epoch)) yield break;
            onClosed?.Invoke(failure);
        }

        /// <summary>
        /// 照檔頭判斷圖的格式（2026-10-30）：以前一律標 `image/png`，可是 iOS／Android 選圖送出去的都是 JPEG
        /// ⇒ 伺服器照標頭存成 `.png`、下載時也回 `image/png`，內容和副檔名對不上（靠解碼器自己認格式才剛好能用）。
        /// </summary>
        private static string ImageType(byte[] data)
        {
            if (data != null && data.Length >= 12)
            {
                if (data[0] == 0xFF && data[1] == 0xD8) return "image/jpeg";
                if (data[0] == 0x89 && data[1] == 0x50 && data[2] == 0x4E && data[3] == 0x47) return "image/png";
                if (data[0] == 0x52 && data[1] == 0x49 && data[2] == 0x46 && data[3] == 0x46 &&
                    data[8] == 0x57 && data[9] == 0x45 && data[10] == 0x42 && data[11] == 0x50) return "image/webp";
            }
            return "image/png";
        }

        /// <summary>`GET /v1/media?offset=&amp;limit=`（相册，新的在前）。</summary>
        public void GetMedia(int offset, int limit, Action<Reply> done)
        {
            Get("/v1/media?offset=" + Mathf.Max(0, offset) + "&limit=" + Mathf.Max(1, limit), done);
        }

        /// <summary>
        /// 抓一張圖回來（相册縮圖／氣泡／全螢幕）；失敗回 null，不拋。
        /// 相冊的圖 id 每次上傳都是新的、內容不會變 ⇒ 本機有就直接用（當場回呼），沒有才下載並存起來。
        /// </summary>
        /// <param name="width">要縮圖就給寬（伺服器只認 <see cref="ThumbWidth"/>）；0＝原圖（全螢幕看圖用）。</param>
        public void GetMediaTexture(string mediaId, Action<Texture2D> done, int width = 0)
        {
            if (string.IsNullOrEmpty(mediaId)) { done?.Invoke(null); return; }
            if (SocialRepository.IsPendingMedia(mediaId))
            {
                // 離線佔位圖：還沒上傳，直接從端末那份解（見 NextOutboxMedia）
                done?.Invoke(Decode(NextOutboxMedia.Read(mediaId)));
                return;
            }
            var path = "/v1/media/" + UnityWebRequest.EscapeURL(mediaId) + (width > 0 ? "?w=" + width : "");
            var cached = CachedBytes(path, "");
            if (cached != null)
            {
                var texture = Decode(cached);
                if (texture != null) { done?.Invoke(texture); return; }
                Cache.Remove(path);                       // 存壞了 ⇒ 丟掉重抓
            }
            GetBytes(path, data => done?.Invoke(data == null ? null : Decode(data)));
        }

        /// <summary>位元組 → 貼圖（解不開 ⇒ null）；CPU 那份不留（只給 UI 顯示）。</summary>
        private static Texture2D Decode(byte[] data)
        {
            if (data == null || data.Length == 0) return null;
            var texture = new Texture2D(2, 2, TextureFormat.RGBA32, false);
            if (texture.LoadImage(data, true)) return texture;
            Destroy(texture);
            return null;
        }

        // ── 表情包（GET App Server /v1/stickers）────────────────────────────────

        /// <summary>這個角色能用的客戶端素材：`{stickers: [{id, label, meaning, url, hash}]}`。</summary>
        /// <remarks>`etag`＝本機那份清單的 `version`：線上沒變 ⇒ 304（<see cref="Reply.NotModified"/>）。</remarks>
        public void GetStickers(string entity, Action<Reply> done, string etag = "")
        {
            GetIfChanged("/v1/stickers?entity=" + UnityWebRequest.EscapeURL(entity ?? ""), etag, done);
        }

        /// <summary>
        /// 抓一個伺服器上的檔（表情包的圖：`url` 是 `/v1/stickers/…`）；失敗回 null，不拋。
        /// 回位元組而不是貼圖：同一張會在很多顆氣泡裡出現，呼叫端快取位元組、每顆氣泡自己建貼圖（各自釋放）。
        /// </summary>
        /// <remarks>
        /// 有快取：帶本機那份的雜湊去問（`If-None-Match`）⇒ 304 就回本機那份；200 回來的連同 `ETag` 存起來。
        /// </remarks>
        public Coroutine GetBytes(string path, Action<byte[]> done)
        {
            return StartCoroutine(BytesRoutine(path, done, true));
        }

        /// <summary>相冊縮圖要多寬（伺服器 `media/thumbs.py` 也只收這幾個寬，免得快取被亂給的寬撐爆）。</summary>
        public const int ThumbWidth = 360;

        /// <summary>
        /// 抓圖（表情包、相冊、全螢幕）同時在飛的上限。表情盤 2026-10-29 踩過：一次開 104 個請求 ⇒ 大半連回應都沒有；
        /// 相冊一頁 30 張以前也是同時全開 ⇒ 在這裡統一排隊（NextStickers 自己那條佇列照留，兩層不衝突）。
        /// </summary>
        private const int MaxBytesInFlight = 6;
        private int _bytesInFlight;

        private IEnumerator BytesRoutine(string path, Action<byte[]> done, bool conditional)
        {
            while (_bytesInFlight >= MaxBytesInFlight) yield return null;
            _bytesInFlight++;
            try
            {
                yield return FetchBytes(path, done, conditional);
            }
            finally
            {
                _bytesInFlight--;
            }
        }

        private IEnumerator FetchBytes(string path, Action<byte[]> done, bool conditional)
        {
            var epoch = Session.Epoch;
            var cache = Cache;
            byte[] data = null;
            var status = 0;
            var etag = "";
            using (var request = new UnityWebRequest(BaseUrl + path, UnityWebRequest.kHttpVerbGET))
            {
                request.SetRequestHeader("Authorization", "Bearer " + Token);
                request.SetRequestHeader("X-Source", Source);
                var known = conditional && cache != null ? cache.EtagOf(path) : "";
                if (!string.IsNullOrEmpty(known)) request.SetRequestHeader("If-None-Match", "\"" + known + "\"");
                request.downloadHandler = new DownloadHandlerBuffer();
                request.timeout = 20;
                yield return request.SendWebRequest();
                status = (int)request.responseCode;
                if (status == 200 && request.downloadHandler != null) data = request.downloadHandler.data;
                etag = request.GetResponseHeader("ETag") ?? "";
            }
            if (!Settle(status, epoch)) yield break;
            if (status == 304)
            {
                data = cache?.Get(path);
                // 說沒變、本機那份卻不見了（剛被清掉）⇒ 不帶雜湊再抓一次（名額已經拿著，直接抓）
                if (data == null) { yield return FetchBytes(path, done, false); yield break; }
            }
            else if (data != null && data.Length > 0 && cache != null && ReferenceEquals(cache, Cache))
            {
                // 沒有 `ETag`（被代理丟了）⇒ 自己算（跟伺服器同一個算法），不然存成空雜湊，
                // 之後跟清單的 `hash` 永遠對不上 ⇒ 每次都重抓
                cache.Put(path, data, string.IsNullOrEmpty(etag) ? SyncCache.ContentTag(data) : etag);
            }
            done?.Invoke(data);
        }

        // ── 單人通話：手機走 App Server；MR 保持舊 Agent 控制路徑 ─────────────────

        /// <summary>`call`／`ping`／`interrupt`／`hangup`。手機使用平台 `/v1/calls`。</summary>
        public void Call(string action, string entity, string callId, Action<Reply> done)
        {
            var body = new JObject().Put("entity", entity ?? "");
            if (Source == "mr")
            {
                if (!string.IsNullOrEmpty(callId)) body.Put("call_id", callId);
                Post("/api/voice/" + action, body, done);
                return;
            }
            if (action == "call")
            {
                body.Put("language", NextConfig.Language);
                Post("/v1/calls", body, done);
                return;
            }
            var root = "/v1/calls/" + UnityWebRequest.EscapeURL(callId ?? "");
            // `state` 是 GET（伺服器沒有 POST 這一支）：以前一律 POST ⇒ 404，響鈴輪詢都當「沒了」（2026-11-17 模擬器實測）
            if (action == "state") { Get(root + "/state", done); return; }
            Post(root + "/" + action, body, done);
        }

        /// <summary>100 ms、16 kHz mono PCM16LE 小帧；调用方必须串行发送并递增 seq。</summary>
        public void SendCallFrame(string callId, int seq, byte[] pcm, Action<Reply> done)
        {
            var body = new JObject().Put("seq", seq).Put("audio", Convert.ToBase64String(pcm ?? new byte[0]));
            Post("/v1/calls/" + UnityWebRequest.EscapeURL(callId ?? "") + "/frames", body, done);
        }

        /// <summary>
        /// 一次送多幀（2026-11-08「像打電話」Phase 1）：手機每 100 ms 一幀，但一幀一請求
        /// 在抖動／慢回包下追不上（實測 450 ms/幀 ⇒ 6.5 秒積壓 50 幀被保險絲掛斷）。
        /// 批量後請求數降到 1/4，服務端照舊逐幀處理（seq 必須連續）。
        /// </summary>
        public void SendCallFrames(string callId, IList<byte[]> pcms, int firstSeq, Action<Reply> done)
        {
            var frames = new JArray();
            for (var i = 0; i < pcms.Count; i++)
            {
                frames.Put(new JObject()
                    .Put("seq", firstSeq + i)
                    .Put("audio", Convert.ToBase64String(pcms[i] ?? new byte[0])));
            }
            var body = new JObject().Put("frames", frames).Put("seq", firstSeq);
            Post("/v1/calls/" + UnityWebRequest.EscapeURL(callId ?? "") + "/frames", body, done);
        }

        public Coroutine CallEvents(string callId, int after, Action<string, JObject> onFrame,
                                    Action<string> onClosed)
        {
            return StartCoroutine(CallEventsRoutine(callId, after, onFrame, onClosed));
        }

        public void CancelCallEvents()
        {
            var request = _callEventsRequest.Take();
            if (request != null) request.Abort();
        }

        private IEnumerator CallEventsRoutine(string callId, int after,
                                              Action<string, JObject> onFrame, Action<string> onClosed)
        {
            var epoch = Session.Epoch;
            var status = 0;
            var failure = "";
            var path = "/v1/calls/" + UnityWebRequest.EscapeURL(callId ?? "") +
                       "/events?after=" + Mathf.Max(0, after);
            using (var request = new UnityWebRequest(BaseUrl + path, UnityWebRequest.kHttpVerbGET))
            {
                _callEventsRequest.Set(request);
                try
                {
                    request.SetRequestHeader("Authorization", "Bearer " + Token);
                    request.SetRequestHeader("Accept", "text/event-stream");
                    request.downloadHandler = new SseDownloadHandler(wires =>
                    {
                        if (Session.IsCurrent(epoch)) Dispatch(wires, onFrame);
                    });
                    request.timeout = 75;
                    yield return request.SendWebRequest();
                    status = (int)request.responseCode;
                    if (status >= 400)
                    {
                        var raw = request.downloadHandler is SseDownloadHandler handler ? handler.Text : "";
                        failure = "call events " + status + (string.IsNullOrEmpty(raw) ? "" : " " + Compact(raw));
                    }
                    else if (request.result != UnityWebRequest.Result.Success)
                    {
                        failure = "call events transport: " + request.error;
                    }
                }
                finally
                {
                    _callEventsRequest.ClearIf(request);
                }
            }
            if (!Settle(status, epoch)) yield break;
            onClosed?.Invoke(failure);
        }

        /// <summary>`GET /api/turns/&lt;id&gt;/audio`：那一輪合併後的 wav（沒有就 404）。</summary>
        public void GetTurnAudio(string turnId, Action<byte[]> onAudio, Action<Reply> onFail)
        {
            StartCoroutine(TurnAudioRoutine(turnId, onAudio, onFail));
        }

        private IEnumerator TurnAudioRoutine(string turnId, Action<byte[]> onAudio, Action<Reply> onFail)
        {
            var epoch = Session.Epoch;
            var reply = new Reply();
            byte[] data = null;
            var path = "/api/turns/" + UnityWebRequest.EscapeURL(turnId) + "/audio";
            var cache = Cache;
            var known = cache != null ? cache.EtagOf(path) : "";
            using (var request = new UnityWebRequest(BaseUrl + path, UnityWebRequest.kHttpVerbGET))
            {
                request.SetRequestHeader("Authorization", "Bearer " + Token);
                request.SetRequestHeader("X-Source", Source);
                request.SetRequestHeader("X-Audio-Profile", "mulaw-v1");
                if (!string.IsNullOrEmpty(known)) request.SetRequestHeader("If-None-Match", "\"" + known + "\"");
                request.downloadHandler = new DownloadHandlerBuffer();
                request.timeout = 30;
                yield return request.SendWebRequest();
                reply.Status = (int)request.responseCode;
                if (reply.Status == 200 && request.downloadHandler != null) data = request.downloadHandler.data;
                reply.Etag = SyncCache.NormalizeEtag(request.GetResponseHeader("ETag"));
                if (!string.IsNullOrEmpty(request.error)) reply.Error = request.error;
            }
            if (!Settle(reply.Status, epoch)) yield break;
            if (reply.Status == 304) data = cache?.Get(path);
            if (data != null && data.Length > 0)
            {
                if (reply.Status == 200 && cache != null && ReferenceEquals(cache, Cache) && !string.IsNullOrEmpty(reply.Etag))
                    cache.Put(path, data, reply.Etag);
                if (onAudio != null) onAudio(data);
            }
            else if (onFail != null) onFail(reply);
        }

        /// <summary>
        /// 回應回來之後的共同收口：登入中這一段的 401 ⇒ 叫 `Unauthorized`（一次）；
        /// 回 false ＝ 出發時的那段登入已經結束（登出／換帳號），回呼不准再動任何東西。
        /// </summary>
        private bool Settle(int status, int epoch)
        {
            if (Session.ReportStatus(status, epoch) && Unauthorized != null) Unauthorized();
            return Session.IsCurrent(epoch);
        }

        /// <summary>取消在途的那一輪（換角色／登出）：它的收尾回呼由呼叫端的票據擋掉。</summary>
        public void CancelTurn()
        {
            var request = _turnRequest.Take();
            if (request != null) request.Abort();
        }

        /// <summary>位置上報走獨立請求槽：停止位置循環時，連已發出的 POST 一起取消（U-3）。</summary>
        public void PostLocation(JObject body, Action<Reply> done)
        {
            CancelLocation();
            StartCoroutine(Routine("/v1/me/location", UnityWebRequest.kHttpVerbPOST, body, 30, done,
                                   _locationRequest));
        }

        public void CancelLocation()
        {
            var request = _locationRequest.Take();
            if (request != null) request.Abort();
        }

        private IEnumerator Routine(string path, string method, JObject body, int timeout, Action<Reply> done,
                                    OwnedSlot<UnityWebRequest> owner = null, string ifNoneMatch = "")
        {
            var epoch = Session.Epoch;
            var reply = new Reply();
            using (var request = new UnityWebRequest(BaseUrl + path, method))
            {
                owner?.Set(request);
                try
                {
                    request.SetRequestHeader("Authorization", "Bearer " + Token);
                    request.SetRequestHeader("X-Source", Source);
                    request.SetRequestHeader("Accept", "application/json");
                    request.SetRequestHeader("Accept-Encoding", "gzip");
                    if (!string.IsNullOrEmpty(ifNoneMatch))
                        request.SetRequestHeader("If-None-Match", "\"" + ifNoneMatch + "\"");
                    request.downloadHandler = new DownloadHandlerBuffer();
                    request.timeout = timeout;
                    if (body != null)
                    {
                        var payload = Encoding.UTF8.GetBytes(body.ToString());
                        request.uploadHandler = new UploadHandlerRaw(payload);
                        request.SetRequestHeader("Content-Type", "application/json; charset=utf-8");
                    }
                    yield return request.SendWebRequest();
                    reply.Status = (int)request.responseCode;
                    reply.Body = request.downloadHandler != null ? request.downloadHandler.text : "";
                    reply.Etag = SyncCache.NormalizeEtag(request.GetResponseHeader("ETag"));
                    if (!string.IsNullOrEmpty(request.error)) reply.Error = request.error;
                    if (!string.IsNullOrEmpty(reply.Body))
                    {
                        try { reply.Json = new JObject(reply.Body); }
                        catch (Exception) { reply.Json = null; }
                    }
                    if (reply.Status == 0 && string.IsNullOrEmpty(reply.Error)) reply.Error = "no response";
                }
                finally
                {
                    // 中途丟例外也要清：不然槽裡留著已 Dispose 的請求，下一次取消就對它 Abort 而炸掉（複檢 2026-10-28）
                    owner?.ClearIf(request);
                }
            }
            if (!Settle(reply.Status, epoch)) yield break;
            if (done != null) done(reply);
        }

        // ── 一輪（POST /api/turn，SSE）────────────────────────────────────────────

        /// <summary>
        /// 送一句。`onFrame(event, data)` 會拿到契約裡每個 kind（`accepted`／`delta`／`speech`／
        /// `done`／`result`／…）。`onClosed(errorText)` 在連線結束時叫一次（正常結束 ⇒ 空字串）。
        /// </summary>
        /// <remarks>
        /// <paramref name="sticker"/>：送一張表情包（`kind: sticker`；`text` 可空，有字就是「圖＋一句」）。
        /// </remarks>
        public Coroutine Turn(string text, string entity, bool wantVoice,
                              Action<string, JObject> onFrame, Action<string> onClosed, string sticker = "")
        {
            CancelTurn();
            return StartCoroutine(TurnRoutine(text, entity, wantVoice, onFrame, onClosed, sticker));
        }

        /// <summary>
        /// 延時聚合（owner 2026-11-04）：一次送出多筆（文字／照片／表情），回應與 `/api/turn` 同一種 SSE。
        /// `items` 的形狀見 `docs/APP-API-SSE.md`：`{kind: text|photo|sticker, …}`。
        /// </summary>
        public Coroutine TurnItems(JArray items, string entity, bool wantVoice,
                                   Action<string, JObject> onFrame, Action<string> onClosed)
        {
            CancelTurn();
            return StartCoroutine(TurnRoutine("", entity, wantVoice, onFrame, onClosed, "", items));
        }

        /// <summary>
        /// 只上傳、**不起回合**（`POST /v1/media`）：延時聚合先把連發的照片換成 `media_id`，
        /// 等視窗關閉時再一次送出（`/api/image` 是「存圖＋立刻跑一輪」，不合這個用法）。
        /// </summary>
        public void UploadMedia(byte[] image, string entity, Action<JObject> done, Action<string> fail)
        {
            StartCoroutine(UploadMediaRoutine(image, entity, done,
                (status, reason) => { if (fail != null) fail(reason); }));
        }

        /// <summary>同上，失敗時多給 HTTP 狀態碼（0＝連不上）：呼叫端要分「連不上 → 先存端末」與「被拒收」。</summary>
        public void UploadMedia(byte[] image, string entity, Action<JObject> done, Action<int, string> fail)
        {
            StartCoroutine(UploadMediaRoutine(image, entity, done, fail));
        }

        public void CancelMediaUploads()
        {
            var requests = _mediaRequests.ToArray();
            _mediaRequests.Clear();
            foreach (var request in requests)
            {
                if (request != null) request.Abort();
            }
        }

        private IEnumerator UploadMediaRoutine(byte[] image, string entity,
                                               Action<JObject> done, Action<int, string> fail)
        {
            var epoch = Session.Epoch;
            var status = 0;
            var raw = "";
            var succeeded = false;
            using (var request = new UnityWebRequest(BaseUrl + "/v1/media", UnityWebRequest.kHttpVerbPOST))
            {
                _mediaRequests.Add(request);
                try
                {
                    request.SetRequestHeader("Authorization", "Bearer " + Token);
                    request.SetRequestHeader("Content-Type", ImageType(image));
                    if (!string.IsNullOrEmpty(entity)) request.SetRequestHeader("X-Entity", entity);
                    request.uploadHandler = new UploadHandlerRaw(image);
                    request.downloadHandler = new DownloadHandlerBuffer();
                    request.timeout = 60;
                    yield return request.SendWebRequest();
                    status = (int)request.responseCode;
                    raw = request.downloadHandler != null ? request.downloadHandler.text : "";
                    succeeded = request.result == UnityWebRequest.Result.Success && status < 400;
                }
                finally
                {
                    _mediaRequests.Remove(request);
                }
            }
            if (!Settle(status, epoch)) yield break;
            if (!succeeded)
            {
                if (fail != null) fail(status, "media " + status + " " + Compact(raw));
                yield break;
            }
            JObject json = null;
            try { json = new JObject(raw); }
            catch (Exception) { json = null; }
            if (json == null)
            {
                if (fail != null) fail(status, "media parse");
                yield break;
            }
            if (done != null) done(json);
        }

        private IEnumerator TurnRoutine(string text, string entity, bool wantVoice,
                                        Action<string, JObject> onFrame, Action<string> onClosed,
                                        string sticker, JArray items = null)
        {
            var body = new JObject().Put("text", text ?? "");
            if (!string.IsNullOrEmpty(entity)) body.Put("entity", entity);
            if (items != null && items.Length() > 0) body.Put("items", items);
            else if (!string.IsNullOrEmpty(sticker)) body.Put("kind", "sticker").Put("sticker", sticker);
            body.Put("want_voice", wantVoice);
            body.Put("request_id", Guid.NewGuid().ToString("N"));

            var epoch = Session.Epoch;
            var failure = "";
            var status = 0;
            using (var request = new UnityWebRequest(BaseUrl + "/api/turn", UnityWebRequest.kHttpVerbPOST))
            {
                _turnRequest.Set(request);
                try
                {
                    request.SetRequestHeader("Authorization", "Bearer " + Token);
                    request.SetRequestHeader("X-Source", Source);
                    request.SetRequestHeader("Accept", "text/event-stream");
                    // parts-v1：正文只走 part；done 不再重送全文（舊 server 會忽略此標頭，照舊相容）。
                    request.SetRequestHeader("X-Stream-Profile", "parts-v1");
                    request.SetRequestHeader("X-Audio-Profile", "mulaw-v1");
                    request.SetRequestHeader("Content-Type", "application/json; charset=utf-8");
                    request.uploadHandler = new UploadHandlerRaw(Encoding.UTF8.GetBytes(body.ToString()));
                    request.downloadHandler = new SseDownloadHandler(wires =>
                    {
                        if (Session.IsCurrent(epoch)) Dispatch(wires, onFrame);
                    });
                    request.timeout = 300;
                    yield return request.SendWebRequest();

                    status = (int)request.responseCode;
                    if (status >= 400)
                    {
                        // 非 200 時伺服器回的是 JSON 錯誤（契約 §5）⇒ 把那一行原文往上交。
                        var raw = request.downloadHandler is SseDownloadHandler handler ? handler.Text : "";
                        failure = "turn " + status + (string.IsNullOrEmpty(raw) ? "" : " " + Compact(raw));
                    }
                    else if (request.result != UnityWebRequest.Result.Success && status == 0)
                    {
                        failure = "turn transport: " + request.error;
                    }
                }
                finally
                {
                    // 只清自己：被下一輪 Abort 的舊協程晚一幀才收尾，不能把新一輪的句柄清掉；
                    // 放在 finally：中途丟例外也要清，不然槽裡留著已 Dispose 的請求，下一次取消就炸（複檢 2026-10-28）
                    _turnRequest.ClearIf(request);
                }
            }
            if (!Settle(status, epoch)) yield break;
            if (onClosed != null) onClosed(failure);
        }

        /// <summary>把一批 (event, data) 解成 JObject 往上交；壞掉的資料靜默丟掉（契約：未知 kind 要忽略）。</summary>
        private static void Dispatch(List<SseStream.Wire> wires, Action<string, JObject> onFrame)
        {
            if (wires == null || onFrame == null) return;
            foreach (var wire in wires)
            {
                var name = wire.Event;
                var data = wire.Data;
                if (name == "ready" || name == "end" || string.IsNullOrEmpty(data)) continue;
                JObject json = null;
                try { json = new JObject(data); }
                catch (Exception) { json = null; }
                if (json == null) continue;
                onFrame(name, json);
            }
        }

        // ── 語音簡訊（POST /api/audio，SSE）────────────────────────────────────

        /// <summary>
        /// 送一段語音簡訊（16 kHz 單聲道 WAV）。回應與 `/api/turn` 同一種 SSE，
        /// **第一幀是 `transcript`**（契約 §2），之後照常 `delta`／`speech`／`done`。
        ///
        /// 語音是二進位 body，沒有地方放 JSON 的 `want_voice` ⇒ 契約用 header `X-Want-Voice: 1`
        /// 表達「這一輪要出聲」；伺服器照舊只讓 owner 生效（guest 的 header 會被忽略）。
        /// </summary>
        /// <remarks>
        /// <paramref name="callId"/>：單人通話裡的一句（header `X-Call`）。伺服器只認主人、只認現在那一通；
        /// 掛掉了回 410（`call_ended`）。通話中整輪直接串流，聲音全在這條線上（不拆後續交付）。
        /// </remarks>
        public Coroutine SendAudio(byte[] wav, string entity, string language, bool wantVoice,
                                   Action<string, JObject> onFrame, Action<string> onClosed, string callId = "")
        {
            CancelTurn();
            return StartCoroutine(AudioRoutine(wav, entity, language, wantVoice, onFrame, onClosed, callId));
        }

        private IEnumerator AudioRoutine(byte[] wav, string entity, string language, bool wantVoice,
                                         Action<string, JObject> onFrame, Action<string> onClosed, string callId)
        {
            var epoch = Session.Epoch;
            var failure = "";
            var status = 0;
            using (var request = new UnityWebRequest(BaseUrl + "/api/audio", UnityWebRequest.kHttpVerbPOST))
            {
                _turnRequest.Set(request);
                try
                {
                    request.SetRequestHeader("Authorization", "Bearer " + Token);
                    request.SetRequestHeader("X-Source", Source);
                    request.SetRequestHeader("Accept", "text/event-stream");
                    request.SetRequestHeader("X-Audio-Profile", "mulaw-v1");
                    request.SetRequestHeader("Content-Type", "audio/wav");
                    if (!string.IsNullOrEmpty(entity)) request.SetRequestHeader("X-Entity", entity);
                    request.SetRequestHeader("X-Mode", "chat");
                    if (!string.IsNullOrEmpty(language)) request.SetRequestHeader("X-Lang", language);
                    if (wantVoice) request.SetRequestHeader("X-Want-Voice", "1");
                    if (!string.IsNullOrEmpty(callId)) request.SetRequestHeader("X-Call", callId);
                    request.SetRequestHeader("X-Request-Id", Guid.NewGuid().ToString("N"));
                    request.uploadHandler = new UploadHandlerRaw(wav ?? new byte[0]);
                    request.downloadHandler = new SseDownloadHandler(wires =>
                    {
                        if (Session.IsCurrent(epoch)) Dispatch(wires, onFrame);
                    });
                    request.timeout = 300;
                    yield return request.SendWebRequest();

                    status = (int)request.responseCode;
                    if (status >= 400)
                    {
                        var raw = request.downloadHandler is SseDownloadHandler handler ? handler.Text : "";
                        failure = "audio " + status + (string.IsNullOrEmpty(raw) ? "" : " " + Compact(raw));
                    }
                    else if (request.result != UnityWebRequest.Result.Success && status == 0)
                    {
                        failure = "audio transport: " + request.error;
                    }
                }
                finally
                {
                    // 中途丟例外也要清：不然槽裡留著已 Dispose 的請求，下一次取消就對它 Abort 而炸掉（複檢 2026-10-28）
                    _turnRequest.ClearIf(request);
                }
            }
            if (!Settle(status, epoch)) yield break;
            if (onClosed != null) onClosed(failure);
        }

        // ── 常駐推播（GET /api/events，SSE）──────────────────────────────────────

        /// <summary>
        /// 開常駐推播。斷線照契約 §3 退避（1→2→4→…→30 秒）；`onState(connected, text)` 回報狀態。
        /// 401 ⇒ 回報 `unauthorized` 並停止（由上層清 token 回登入頁）。
        /// </summary>
        public void StartEvents(Action<string, JObject> onEvent, Action<bool, string> onState)
        {
            if (_eventsRoutine != null) return;
            _eventsRoutine = StartCoroutine(EventsRoutine(onEvent, onState));
        }

        private IEnumerator EventsRoutine(Action<string, JObject> onEvent, Action<bool, string> onState)
        {
            var epoch = Session.Epoch;
            var backoff = 1f;
            EventsState = "connecting";
            while (true)
            {
                var stop = false;
                using (var request = new UnityWebRequest(BaseUrl + "/v1/events", UnityWebRequest.kHttpVerbGET))
                {
                    _eventsRequest.Set(request);
                    try
                    {
                        request.SetRequestHeader("Authorization", "Bearer " + Token);
                        request.SetRequestHeader("X-Source", Source);
                        request.SetRequestHeader("Accept", "text/event-stream");
                        var cursor = Cursors.Current;
                        if (!string.IsNullOrEmpty(cursor))
                        {
                            request.SetRequestHeader("Last-Event-ID", cursor);
                            Debug.Log("[next-events] Last-Event-ID=" + cursor);
                        }
                        // 同一條連線可能分很多批交付；缺口旗標必須跨批次保留，重連才從舊游標重放。
                        var cursorBlocked = false;
                        request.downloadHandler = new SseDownloadHandler(wires =>
                        {
                            if (wires == null || !Session.IsCurrent(epoch)) return;
                            // 逐幀派送交給共用層（`SseFramePump`）：**處理成功才推進游標**。
                            // ⚠️ 以前是「先 Advance 再 onEvent」⇒ 某幀一拋（實測：切到設定頁時她主動
                            // 說話，UI 撞到已銷毀的物件）整批後面的幀被吃掉、游標又已推過去 ⇒ 永久丟訊息。
                            var frames = new List<SseFrame>(wires.Count);
                            for (var i = 0; i < wires.Count; i++)
                            {
                                frames.Add(new SseFrame
                                {
                                    Id = wires[i].Id,
                                    Event = wires[i].Event,
                                    Data = wires[i].Data,
                                });
                            }
                            SseFramePump.Pump(frames, frame =>
                            {
                                // 契約 §3：`ready` 代表連上了 ⇒ 徽章才是 online（不是「請求送出去」就算）
                                if (frame.Event == "ready")
                                {
                                    backoff = 1f;                // 連上了就從頭算（以前一路翻倍到 30 秒就再也不回來）
                                    EventsState = "online";
                                    if (onState != null) onState(true, "ready");
                                    return false;                // 控制幀：算處理完，但不吃游標
                                }
                                if (string.IsNullOrEmpty(frame.Data)) return true;
                                JObject json;
                                try { json = new JObject(frame.Data); }
                                catch (Exception)
                                {
                                    // 壞包推進：重放同一份位元組也是壞的，擋在這裡只會卡住整條線
                                    return true;
                                }
                                if (onEvent != null) onEvent(frame.Event, json);
                                return true;
                            }, id => Cursors.Advance(id), ref cursorBlocked, (frame, error) =>
                            {
                                FrameErrors++;
                                Debug.LogError("[next-events] 幀處理失敗（游標不前進，重連時會補）："
                                               + frame + " " + error.Message);
                            });
                        });
                        request.timeout = 0;                      // 常駐：不設逾時（改用下面的「沒動靜」看門狗）
                        // ⚠️ 看門狗（2026-10-30）：伺服器每 15 秒送一次 `: ping`。換網路（Wi-Fi ↔ 行動）、
                        // 長時間待機之後，連線可能已經死了、TCP 卻還沒報錯 ⇒ 徽章一直「オンライン」，
                        // 她的主動訊息卻收不到，要等系統的逾時（好幾分鐘）。⇒ 一段時間沒收到任何位元組就自己斷、重連。
                        var stream = (SseDownloadHandler)request.downloadHandler;
                        var op = request.SendWebRequest();
                        var seen = -1;
                        var quietSince = Time.realtimeSinceStartup;
                        while (!op.isDone)
                        {
                            if (stream.Bytes != seen)
                            {
                                seen = stream.Bytes;
                                quietSince = Time.realtimeSinceStartup;
                            }
                            else if (Time.realtimeSinceStartup - quietSince > EventsSilenceLimit)
                            {
                                Debug.Log("[next-events] " + EventsSilenceLimit + " 秒沒有動靜 ⇒ 斷開重連");
                                request.Abort();
                                break;
                            }
                            yield return null;
                        }

                        var status = (int)request.responseCode;
                        if (status == 401)
                        {
                            EventsState = "unauthorized";
                            if (onState != null) onState(false, "unauthorized");
                            Settle(status, epoch);
                            stop = true;
                        }
                        else
                        {
                            if (status == 409)
                            {
                                // cursor_expired：清掉才重連；帶著同一個舊 id 只會永遠卡在 409。
                                Cursors.Reset();
                                ChatSync.SyncConversations();
                            }
                            // 斷了（不管之前有沒有 ready 過）⇒ 進退避重連
                            if (EventsState == "online") Reconnects++;
                            EventsState = "reconnecting";
                            if (onState != null) onState(false, string.IsNullOrEmpty(request.error)
                                ? "closed " + status : request.error);
                        }
                    }
                    finally
                    {
                        // 中途丟例外也要清：不然槽裡留著已 Dispose 的請求，下一次取消就對它 Abort 而炸掉（複檢 2026-10-28）
                        _eventsRequest.ClearIf(request);
                    }
                }
                if (stop) { _eventsRoutine = null; yield break; }
                yield return new WaitForSeconds(backoff);
                backoff = Mathf.Min(30f, backoff * 2f);
            }
        }

        /// <summary>常駐推播多久沒收到任何東西就當它死了（伺服器 15 秒一次 ping ⇒ 連掉三次）。</summary>
        private const float EventsSilenceLimit = 45f;

        public void StopEvents()
        {
            var request = _eventsRequest.Take();
            if (request != null) request.Abort();
            if (_eventsRoutine != null) StopCoroutine(_eventsRoutine);
            _eventsRoutine = null;
            EventsState = "off";
        }

        private void OnDestroy()
        {
            CancelTurn();
            CancelLocation();
            CancelCallEvents();
            StopEvents();
            _socialSync?.Cancel();
            _chatSync?.Cancel();
            Social?.Dispose();
            Chat?.Dispose();
            Social = null;
            Chat = null;
        }

        private static string Compact(string raw)
        {
            var text = raw.Replace("\r", " ").Replace("\n", " ").Trim();
            return text.Length > 160 ? text.Substring(0, 160) : text;
        }
    }
}
