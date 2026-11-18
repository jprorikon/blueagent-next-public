using System.Collections.Generic;

namespace BlueAgent.Logic
{
    /// <summary>
    /// 橋的 `/chat` 契約——**規則在這裡，傳輸不在這裡**（對照 Kotlin 版的 `BridgeProtocol.kt`）。
    ///
    /// 為什麼只搬規則（owner 2026-10-03 選 A 案）：兩端的傳輸本質不同（安卓 OkHttp、iOS `URLSession`
    /// 手寫 SSE 迴圈），但**請求長什麼樣、幀怎麼解**這兩端一模一樣——而它們正是最容易分叉的地方
    /// （2026-10-02 實測踩過：iOS 忘了帶 `soul`，她改用預設人格回中文）。所以在這裡放：payload 組法、
    /// 共用 headers、SSE 事件→<see cref="Frame"/>、以及 `speech`／`media` 幀的解碼；各端的連線、
    /// 逾時、重試留在各端。
    ///
    /// Frames（橋的契約）：`read` / `started` / `delta` / `thinking` / `tool` / `media` / `speech` /
    /// `done` / `error`，見 `blueagent-server` 的 README。
    ///
    /// ⚠️ `audio` 這一格**不在這裡解 base64**：common 不碰平台的編碼 API ⇒
    /// <see cref="Speech.AudioBase64"/> 原樣帶出去，由平台解。
    ///
    /// 命名刻意沿用 Kotlin 側（方法名 PascalCase、欄位名照抄 `runId`／`ownerEnd`／`lateMin`…）——
    /// 移植過來的呼叫端要一眼看得懂，名字一改就會多出一次「悄悄走鐘」的機會。
    ///
    /// **文字語意一律走 <see cref="KotlinText"/>**（`Trim`／`IsWhitespace`）：Kotlin 的
    /// `isNotBlank()` 把 U+0085（NEL）、U+180E 當「有值」，而 C# 的 `string.IsNullOrWhiteSpace`
    /// 剛好相反 ⇒ **不可以用 .NET 那一套**（fixture 已把這兩顆地雷釘死）。
    /// **JSON 一律走 <see cref="JObject"/>**（不要自己寫 trim 或 double 格式）。
    ///
    /// 等價性由 `fixtures/bridge_protocol.json` 逐條對拍（Kotlin 那一側機器產生的真值）。
    /// </summary>
    public static class BridgeProtocol
    {
        /// <summary>
        /// 嘴型的一格：<see cref="AtMs"/> 是**相對這一句起點**的毫秒。刻意不重用 app 的
        /// `LipTrack.Frame`——那個檔要用 `java.io.File`（JVM 專屬），協定層不該把它拖進來。
        /// </summary>
        public sealed class Motion
        {
            public Motion(int atMs, string shape) { AtMs = atMs; Shape = shape; }
            public int AtMs { get; }
            public string Shape { get; }
        }

        /// <summary>橋側的一句語音（`speech` 幀）。</summary>
        public sealed class Speech
        {
            public Speech(int index, int total, string text, double seconds, string audioBase64,
                          List<Motion> motion, bool last)
            {
                Index = index; Total = total; Text = text; Seconds = seconds;
                AudioBase64 = audioBase64; Motion = motion; Last = last;
            }

            public int Index { get; }
            public int Total { get; }
            public string Text { get; }
            public double Seconds { get; }

            /// <summary>base64 的 WAV，**原樣**（見類別註解的檔頭說明）。</summary>
            public string AudioBase64 { get; }

            /// <summary>`(ms, shape)` 依序；壞掉／空的列會被跳過（不讓一列壞掉吃掉整句）。</summary>
            public List<Motion> Motion { get; }

            public bool Last { get; }
        }

        /// <summary>橋側搬好的一件產物（`media` 幀；打字與耳朵兩條路都會送）。</summary>
        public sealed class Media
        {
            public Media(string kind, string url, string name, long bytes, bool replay)
            {
                Kind = kind; Url = url; Name = name; Bytes = bytes; Replay = replay;
            }

            public string Kind { get; }
            public string Url { get; }
            public string Name { get; }
            public long Bytes { get; }
            public bool Replay { get; }
        }

        /// <summary>
        /// 對話的一回合（`GET /v1/history` 的 `turns[i]`，owner 2026-10-06）。
        ///
        /// `Role` = `user`／`assistant`；`Content` ＝那一段話。**橋只給這兩格**（沒有來源標記、
        /// 沒有 ts）⇒ 手機側不要去猜「這是不是 VC 講的」，一律用同一個淡色標示即可。
        /// </summary>
        public sealed class Turn
        {
            public Turn(string role, string content) { Role = role; Content = content; }
            public string Role { get; }
            public string Content { get; }
        }

        /// <summary>
        /// **代播**的一句（`narration` 幀，owner 2026-10-04）：橋事先烘培好的固定台詞（分角色）＋口型。
        ///
        /// 為什麼與 <see cref="Speech"/> 分開、不共用 `index`／`total`：這是「她在做事時代替她說的
        /// 一句話」，**不進回覆**——不寫紀錄、不參與回覆的唸出進度（app 側的 `bridgeNextIndex` 是
        /// 嚴格的連續序，插一段進去會把回覆的語音全判成殘幀丟掉）。所以它有自己的通道。
        ///
        /// ⚠️ 屬性叫 `Character` 而不是 `Char`：`char` 是 C 的型別名，Kotlin/Native 匯出到 ObjC 時
        /// 會改名（Swift 看到的是 `char_`）⇒ iOS 那一側編不過（2026-10-04 真踩）。**線上 JSON 的
        /// 鍵仍是 `"char"`**（那是橋的契約）——這裡只是名字。
        /// </summary>
        public sealed class Narration
        {
            public Narration(string character, string clip, string text, double seconds,
                             string audioBase64, List<Motion> motion)
            {
                Character = character; Clip = clip; Text = text; Seconds = seconds;
                AudioBase64 = audioBase64; Motion = motion;
            }

            public string Character { get; }
            public string Clip { get; }
            public string Text { get; }
            public double Seconds { get; }
            public string AudioBase64 { get; }
            public List<Motion> Motion { get; }
        }

        /// <summary>
        /// リマインドの一本（`remind` 幀、owner 2026-10-06「（聊天模式可用）提醒功能」）。
        ///
        /// 橋が「その時刻になったよ」と**彼女の方から**屆けるもの：<see cref="Text"/>＝用件
        /// （紀錄に殘る一言）、<see cref="Say"/>＝彼女が言う一句（この句は別の `narration` 幀で
        /// 聲にもなる）。<see cref="LateMin"/>＝定刻より何分遲れて屆いたか（0＝定刻どおり）。
        ///
        /// ⚠️ <see cref="Speech"/> とは別物：あれは「回覆の一句」（`index`／`total` の嚴格な連續序に
        /// 載る）で、これは**回覆ではない**獨自の出來事。
        /// </summary>
        public sealed class Reminder
        {
            public Reminder(string id, string text, string say, double at, string clock, int lateMin)
            {
                Id = id; Text = text; Say = say; At = at; Clock = clock; LateMin = lateMin;
            }

            public string Id { get; }
            public string Text { get; }
            public string Say { get; }
            public double At { get; }
            public string Clock { get; }
            public int LateMin { get; }
        }

        /// <summary>
        /// 一幀。<see cref="Kind"/> 決定哪些欄位有意義：
        /// `read`→<see cref="Src"/>｜`started`→<see cref="RunId"/>/<see cref="Session"/>/
        /// <see cref="Voice"/>｜`delta`・`thinking`→<see cref="Text"/>｜`tool`→<see cref="Tool"/>/
        /// <see cref="State"/>/<see cref="Preview"/>｜`speech`→<see cref="Speech"/>｜
        /// `media`→<see cref="Media"/>｜`done`→<see cref="Text"/>/<see cref="RunId"/>/
        /// <see cref="Usage"/>｜`error`→<see cref="Message"/>。
        /// </summary>
        public sealed class Frame
        {
            public Frame(string kind, string src = "", string runId = "", string session = "",
                         bool voice = false, string text = "", string tool = "", string state = "",
                         string preview = "", string message = "", string ownerEnd = "", string usage = "",
                         Speech speech = null, Narration narration = null, Media media = null,
                         Reminder reminder = null)
            {
                Kind = kind; Src = src; RunId = runId; Session = session; Voice = voice; Text = text;
                Tool = tool; State = state; Preview = preview; Message = message; OwnerEnd = ownerEnd;
                Usage = usage; Speech = speech; Narration = narration; Media = media; Reminder = reminder;
            }

            public string Kind { get; }
            public string Src { get; }
            public string RunId { get; }
            public string Session { get; }
            public bool Voice { get; }
            public string Text { get; }
            public string Tool { get; }
            public string State { get; }
            public string Preview { get; }
            public string Message { get; }

            /// <summary>`owner` 幀（見 <see cref="ParseEvent"/>）：現在誰在聽——`phone`／`discord`／`""`。</summary>
            public string OwnerEnd { get; }

            /// <summary>`done` 的 `usage`：**原樣 JSON 字串**（平台自己解，common 不假設它的形狀）。</summary>
            public string Usage { get; }

            public Speech Speech { get; }
            public Narration Narration { get; }
            public Media Media { get; }
            public Reminder Reminder { get; }
        }

        /// <summary>
        /// `POST /game` 的回應：`ok`／`error`／`game` 那一幀（<see cref="Frame"/>，原樣 JSON 字串），
        /// **外加** `op=talk` 的 `voice`（她要出聲時那一句的 <see cref="Speech"/> 形狀，用**同一個**
        /// <see cref="DecodeSpeech"/> 解；沒要／失敗＝null）。
        /// </summary>
        public sealed class GameReply
        {
            public GameReply(bool ok, string error, string frame, Speech voice)
            {
                Ok = ok; Error = error; Frame = frame; Voice = voice;
            }

            public bool Ok { get; }
            public string Error { get; }

            /// <summary>那一幀的**原樣 JSON 字串**——盤面怎麼畫是頁面的事，這一層不重新建模。</summary>
            public string Frame { get; }

            public Speech Voice { get; }
        }

        // ── payload 組法 ────────────────────────────────────────────────────────

        /// <summary>
        /// `POST /chat` 的 body。`session`／`soul`／`mode` 空的不帶；`tts` 一定帶（`1`／`0`）。
        ///
        /// ⚠️ `soul` 沒帶＝**人格完全沒進 prompt**（橋用它在橋側組人格區塊）——2026-10-02 iOS
        /// 忘了帶，她就用預設助手人格回中文。這一條現在是共用規則，不會再各端各忘一次。
        ///
        /// 「看截图」（owner 2026-10-03）：圈選好的畫面（base64 JPEG，**不帶 data: 前綴**）。
        /// 空的不帶 ⇒ 沒圖時這條路與舊版一字不差。文字可以空（只有圖）——橋那一側已放寬。
        /// </summary>
        public static string ChatPayload(string text, string session = "", string soul = "",
                                         string mode = "", bool tts = false, string image = "")
        {
            var payload = new JObject().Put("text", text);
            if (IsNotBlank(session)) payload.Put("session", session);
            if (IsNotBlank(soul)) payload.Put("soul", soul);
            if (IsNotBlank(mode)) payload.Put("mode", mode);
            payload.Put("tts", tts ? 1 : 0);
            if (IsNotBlank(image)) payload.Put("image", image);
            return payload.ToString();
        }

        /// <summary>
        /// `POST /v1/loc` 的 body（owner 2026-10-03「改 app 请求 gps 权限，然后结合 gps 走天气」）。
        ///
        /// ⚠️ 這條**不經會話**（不帶 `session`／`soul`／`mode`）：位置是**端末的屬性**，不是某一段
        /// 對話的——與 `X-Device`（偏好／記憶的索引鍵）同一種地位。橋用 header 的 `X-Device` 決定
        /// 寫到哪一台，天氣那顆橋內工具讀它；沒有座標時它自己退回 IP 概算。
        /// `acc`＝精度（米），`0`＝不帶（＝未知）。
        /// </summary>
        public static string LocPayload(double lat, double lon, float acc = 0f)
        {
            var payload = new JObject().Put("lat", lat).Put("lon", lon);
            if (acc > 0f) payload.Put("acc", (double)acc);
            return payload.ToString();
        }

        /// <summary>
        /// `POST /v1/ends/settings` 的 body（owner 2026-10-06）：端末**主動把當前設定推給橋**。
        ///
        /// 為什麼要推：橋的 persona（角色／模式／說話開關）以前只能靠猜或靠 `ENDS_BIND_CHARACTER`
        /// 手動兜底。端末自己把權威值送上去之後，Discord 端跟隨時就有正確的角色與聲線；
        /// **session 改變時（進／出對局、進／出 TRPG 桌、換新對話）也要一起推**。
        ///
        /// `end` 可省（預設＝橋那一側的 `phone`）；亦可走 header `X-End`。空字串欄位不帶 ⇒ 舊版橋
        /// 不受影響（契約 additive）。
        /// </summary>
        public static string EndsSettingsPayload(string device, string session, string character,
                                                 string mode, bool tts, string end = "")
        {
            var payload = new JObject().Put("device", device);
            if (IsNotBlank(session)) payload.Put("session", session);
            if (IsNotBlank(character)) payload.Put("character", character);
            if (IsNotBlank(mode)) payload.Put("mode", mode);
            payload.Put("tts", tts ? 1 : 0);
            // `end` 省掉時橋自己當 `phone`；非綁定端末推 `discord` 會被橋回 403（呼叫端靜默即可）。
            if (IsNotBlank(end)) payload.Put("end", end);
            return payload.ToString();
        }

        /// <summary>
        /// `POST /v1/ends/claim` 的 body——**本端末現在要耳朵**（owner 2026-10-16）。
        ///
        /// 為什麼要送（先前只有 App 那一側會送）：`/v1/ends` 的 `owner` 是**橋的事實**，
        /// 而這一側的「呼喚」按下去只是本地改 `micOn` ⇒ 橋不知道，下一次拉取又把它改回去。
        /// `explicit=1` ＝ 從對方手上搶得走（App 的按鈕／Discord 進頻道那一級），
        /// 對應橋側 `claim(session, end, character, explicit=True)`。
        /// `end` 省掉時橋自己當 `phone`（這一側就是 `phone` 入口）。
        /// </summary>
        public static string EndsClaimPayload(string device, string session, string character,
                                              string end = "", bool exclusive = true)
        {
            var payload = new JObject().Put("device", device);
            if (IsNotBlank(session)) payload.Put("session", session);
            if (IsNotBlank(character)) payload.Put("character", character);
            if (IsNotBlank(end)) payload.Put("end", end);
            if (exclusive) payload.Put("explicit", 1);
            return payload.ToString();
        }

        /// <summary>
        /// `POST /v1/ends/channel` 的 body（owner 2026-10-06：「能不能在手機上面選頻道」）。
        ///
        /// App 下指令，要 **Discord 端**（`end="discord"`）切到 `channel_id` 那個語音頻道。
        /// 橋只寫綁定的 `requested_channel_id`，真正 `move_to` 由 Discord 端下一輪輪詢做。
        /// 授權在橋側（只准綁定端末／該角色 owner 端），非綁定端末回 403（呼叫端靜默即可）。
        /// </summary>
        public static string EndsChannelPayload(string device, string channelId, string channelName,
                                                string character = "")
        {
            var payload = new JObject().Put("end", "discord").Put("device", device)
                .Put("channel_id", channelId);
            if (IsNotBlank(channelName)) payload.Put("channel_name", channelName);
            if (IsNotBlank(character)) payload.Put("character", character);
            return payload.ToString();
        }

        /// <summary>
        /// `GET /v1/history` 的本體 → 回合列表（owner 2026-10-06「手機上看得到 Discord 裡聊過的內容」）。
        ///
        /// 只讀、**只認 `role`／`content` 兩格**（橋不回來源標記、也不回 ts）⇒ 這裡不做任何來源
        /// 推論；去重與顯示都在頁面。不是 JSON／`ok` 不是 true ⇒ 空列表（＝畫面不變）。
        /// </summary>
        public static List<Turn> HistoryTurns(string body)
        {
            var outp = new List<Turn>();
            JObject obj;
            // 壞掉的 JSON ⇒ 空列表（Kotlin 那側是 `runCatching { … }.getOrNull() ?: return emptyList()`）。
            try { obj = new JObject(KotlinText.Trim(body)); }
            catch { return outp; }
            if (!obj.OptBoolean("ok")) return outp;
            var arr = obj.OptJArray("turns");
            if (arr == null) return outp;
            for (var i = 0; i < arr.Length(); i++)
            {
                var row = arr.OptJObject(i);
                if (row == null) continue;
                var role = KotlinText.Trim(row.OptString("role"));
                var content = row.OptString("content");
                if (role.Length == 0 || content.Length == 0) continue;
                outp.Add(new Turn(role, content));
            }
            return outp;
        }

        /// <summary>
        /// 共用的 request headers（Bearer ＋ `X-Device`；`sse=true` 才帶 `Accept: text/event-stream`）。
        /// 回傳的 List 就是**插入序**（Kotlin 那側是 `linkedMapOf`）——順序是契約的一部分。
        /// </summary>
        public static List<KeyValuePair<string, string>> Headers(string token, string device, bool sse = true)
        {
            var h = new List<KeyValuePair<string, string>>();
            h.Add(new KeyValuePair<string, string>("Authorization", "Bearer " + token));
            h.Add(new KeyValuePair<string, string>("X-Device", device));
            if (sse) h.Add(new KeyValuePair<string, string>("Accept", "text/event-stream"));
            h.Add(new KeyValuePair<string, string>("Content-Type", "application/json; charset=utf-8"));
            return h;
        }

        /// <summary>
        /// `POST /game` 的 body（五子棋，owner 2026-10-03）。**走同一條橋**（不新開服務、不新開連線）。
        ///
        /// 一局棋**與會話無關**（設計 §0-B）⇒ 這裡刻意**不帶** `session`／`soul`／`mode`：棋局不進
        /// 任何對話紀錄、不進上下文、不寫記憶。`op=start` 不帶 `game`（id 由橋生）；`op=move` 要帶
        /// `x`／`y`；`op=close` 只帶 `game`。`difficulty` 不給＝橋的預設（`easy`）。
        ///
        /// ⚠️ `x`／`y` 用 `-1`＝「不帶」，**不用可空 Int**：Kotlin/Native 會把可空 Int 變成 boxed
        /// `KotlinInt`，Swift 那端每次都得自己包一層 ⇒ 兩端的呼叫更容易寫錯。座標本來就不可能負。
        /// </summary>
        public static string GamePayload(string op, string id = "", int x = -1, int y = -1,
                                         string difficulty = "", string previous = "",
                                         string character = "", bool voice = false)
        {
            var payload = new JObject().Put("op", op);
            if (IsNotBlank(id)) payload.Put("game", id);
            if (x >= 0) payload.Put("x", x);
            if (y >= 0) payload.Put("y", y);
            if (IsNotBlank(difficulty)) payload.Put("difficulty", difficulty);
            // 「再來一局」：`op=start` ＋ `previous`＝先收掉舊局（一次往返完成，橋上不會堆沒人收的棋）。
            if (IsNotBlank(previous)) payload.Put("previous", previous);
            // `op=talk`（她按局勢搭話）要用角色組人格。⚠️ 這是**角色 id 不是 session**。
            if (IsNotBlank(character)) payload.Put("character", character);
            // 只有 `op=talk` 有意義：**手機說要才合成**（`speakReplies` 關著時不讓橋白合成一句）。
            if (voice) payload.Put("voice", true);
            return payload.ToString();
        }

        // ── 回應／事件 → 模型 ──────────────────────────────────────────────────

        /// <summary>
        /// `POST /trpg/enter` 的 body（owner 2026-10-05／10-08）。**參與即鎖定**：這一台坐上去
        /// 之後別台就進不來（橋回 409 `room_full` ⇒ 版面對他有意義 ⇒ 照樣進版面、只是把話顯示出來）。
        ///
        /// `table_id`＝選哪一張桌（空＝橋用它記得的）；`char_id`／`mode`／`name`＝**選席位**
        /// （`mode=import` 匯入既有角色／`mode=create` 在這一桌新建；`name` 是席位顯示名
        /// ——不給的話桌會拿 `char_id` 當席位名，實測過）。
        /// </summary>
        public static string TrpgEnterPayload(string character, string tableId = "", string charId = "",
                                              string mode = "", string name = "")
        {
            var payload = new JObject().Put("character", character);
            if (IsNotBlank(tableId)) payload.Put("table_id", tableId);
            if (IsNotBlank(charId))
            {
                payload.Put("char_id", charId);
                payload.Put("mode", IsNotBlank(mode) ? mode : "import");
                if (IsNotBlank(name)) payload.Put("name", name);
            }
            return payload.ToString();
        }

        /// <summary>
        /// `POST /trpg/leave` 的 body：出桌＝**下線**（橋記住她離線 ⇒ 桌再踢過來時只會代打 pass 一手，
        /// 並**當場把這一局落成日記**；只有真的坐上去過的才打）。
        /// </summary>
        public static string TrpgLeavePayload(string character, string tableId = "")
        {
            var payload = new JObject().Put("character", character);
            if (IsNotBlank(tableId)) payload.Put("table_id", tableId);
            return payload.ToString();
        }

        /// <summary>
        /// `POST /trpg/poll` 的 body（真相：進桌之後開始輪詢——**進來那 45 秒 1 秒一次、之後 4 秒一次**）。
        /// `since`＝已經看過的感想序（同一個景的感想不播第二遍）；`tts`＝「說話」開關（橋據此決定
        /// 要不要合成——手機說要才合成，省得橋白做一句）。
        /// </summary>
        public static string TrpgPollPayload(string since, bool tts, string tableId = "")
        {
            var payload = new JObject().Put("since", since ?? "").Put("tts", tts ? 1 : 0);
            if (IsNotBlank(tableId)) payload.Put("table_id", tableId);
            return payload.ToString();
        }

        /// <summary>
        /// `POST /trpg/enter` 的回應 ⇒ `frame` 那一幀的**原樣 JSON 字串**（怎麼畫是畫面的事）。
        /// 進不去（`room_full`）時 `frame` 可能是空的 ⇒ 回空字串（畫面只顯示那句話，不假裝進得去）。
        /// </summary>
        public static string ParseTrpgFrame(string body)
        {
            JObject obj;
            try { obj = new JObject(KotlinText.Trim(body)); }
            catch { return ""; }
            var frame = obj.OptJObject("frame");
            return frame == null ? "" : frame.ToString();
        }

        /// <summary>
        /// `POST /game` 的回應：`ok`／`error`／`game`（那一幀**原樣 JSON 字串**——盤面怎麼畫是頁面的
        /// 事，這一層不重新建模，免得兩端各解一套又分叉）。
        ///
        /// 不合法（越界／已佔／不是他的輪／棋已結束）＝ `ok=false` ＋ `error` ＋ **附上現在的盤面**
        /// （照它重畫就好，不必自己猜哪裡錯了）。不是 JSON ⇒ `error` 帶原文前 120 字（`take(120)`）。
        /// </summary>
        public static GameReply ParseGameReply(string body)
        {
            JObject obj;
            try { obj = new JObject(KotlinText.Trim(body)); }
            catch { return new GameReply(false, "橋回的不是 JSON：" + Take(body, 120), "", null); }
            var game = obj.OptJObject("game");
            var voice = obj.OptJObject("voice");
            return new GameReply(
                obj.OptBoolean("ok", false),
                obj.OptString("error"),
                game == null ? "" : game.ToString(),
                voice == null ? null : DecodeSpeech(voice));
        }

        /// <summary>
        /// 一個 SSE 事件（`event:` 那行 ＋ `data:` 那行的內容）→ <see cref="Frame"/>。
        ///
        /// 不認得的 `event`、爆掉的 JSON、以及**空的 `delta`／`thinking`** 都回 null
        /// （＝這一幀等於沒來）。空字串那一條是實測規則：橋偶爾會送空的 delta，濾掉才不會讓畫面
        /// 出現空的一格。
        /// </summary>
        public static Frame ParseEvent(string @event, string data)
        {
            var kind = KotlinText.Trim(@event);
            if (kind.Length == 0) return null;
            JObject obj;
            try { obj = new JObject(KotlinText.Trim(data)); }
            catch { return null; }

            switch (kind)
            {
                // **嘴型訊框**（真相 webui `app.js:1942` `case 'lip': avatar.applyLip(message.value)`）。
                // 空值＝這一幀沒來（同 delta 那條規則）；形狀對不對交給 `AvatarTimeline` 擋。
                case "lip":
                {
                    var mouth = obj.OptString("value");
                    return mouth.Length == 0 ? null : new Frame("lip", text: mouth);
                }

                case "read":
                    return new Frame("read", src: obj.OptString("src"));

                case "started":
                    return new Frame("started",
                        runId: obj.OptString("run_id"),
                        session: obj.OptString("session"),
                        voice: obj.OptBoolean("voice", false));

                // 空字串＝這一幀沒來（橋偶爾會送空的 delta）。
                case "delta":
                {
                    var t = obj.OptString("text");
                    return t.Length == 0 ? null : new Frame("delta", text: t);
                }

                case "thinking":
                {
                    var t = obj.OptString("text");
                    return t.Length == 0 ? null : new Frame("thinking", text: t);
                }

                // 中途動作報告（owner 2026-10-04）：**橋代播**的一句話（起手／心跳／收尾）——委任期間
                // 腦是阻塞的，所以由橋按事件節奏推。`text` 是橋寫好的句子（不進 i18n：那是橋的聲音）。
                case "progress":
                {
                    var t = obj.OptString("text");
                    return t.Length == 0 ? null : new Frame("progress", text: t);
                }

                // 代播的一句**出聲**（owner 2026-10-04）：事先烘培好的台詞（分角色）＋口型。
                // 有自己的通道：不進回覆、不落紀錄（見 <see cref="Narration"/> 的註解）。沒帶 audio ⇒
                // 當作沒有，呼叫端只寫狀態列（＝與 `progress` 同一種效果）。
                case "narration":
                {
                    var n = DecodeNarration(obj);
                    return n == null ? null : new Frame("narration", text: n.Text, narration: n);
                }

                // リマインド（owner 2026-10-06）：橋が定刻に彼女の方から屆ける一本。**頂層イベント**
                // （`media`／`progress`／`narration` と同じ形）。`text` を載せるのは、舊 app が
                // 「状態列に一行出す」だけでも意味があるようにするため（契約は additive）。
                case "remind":
                {
                    var r = DecodeReminder(obj);
                    if (r == null) return null;
                    return new Frame("remind", text: r.Say.Length > 0 ? r.Say : r.Text, reminder: r);
                }

                // 「看截图」（owner 2026-10-03）：橋側把畫面讀成文字的那一幀（她是照它回答的）。
                // 舊版 app 不認這個 event ⇒ 靜默忽略（契約 additive）。
                case "see":
                    return new Frame("see", text: obj.OptString("text"));

                // 誰在聽（`owner` 幀，owner 2026-10-06）：橋在有訂閱的 session 上廣播「現在誰在聽」
                // （`{"character","end","session","since"}`）。app 用它決定「DC 在聽時手機不收音」。
                case "owner":
                    return new Frame("owner",
                        ownerEnd: obj.OptString("end"),
                        session: obj.OptString("session"));

                case "tool":
                    return new Frame("tool",
                        tool: obj.OptString("tool"),
                        state: obj.OptString("state"),
                        preview: obj.OptString("preview"));

                case "speech":
                {
                    var s = DecodeSpeech(obj);
                    return s == null ? null : new Frame("speech", speech: s);
                }

                case "media":
                {
                    var m = DecodeMedia(obj);
                    return m == null ? null : new Frame("media", media: m);
                }

                case "done":
                {
                    var usage = obj.OptJObject("usage");
                    return new Frame("done",
                        text: obj.OptString("text"),
                        runId: obj.OptString("run_id"),
                        usage: usage == null ? "" : usage.ToString());
                }

                case "error":
                    return new Frame("error", message: obj.OptString("message"));

                default:
                    return null;
            }
        }

        /// <summary>
        /// `speech` 幀 → <see cref="Speech"/>；沒有音訊 ⇒ null（這一幀等於沒來；**壞掉的 base64 由
        /// 平台那一層再判**，免得一幀壞掉讓整輪啞掉）。
        /// </summary>
        public static Speech DecodeSpeech(JObject frame)
        {
            var encoded = frame.OptString("audio");
            if (encoded.Length == 0) return null;
            return new Speech(
                index: frame.OptInt("index"),
                total: frame.OptInt("total"),
                text: frame.OptString("text"),
                seconds: frame.OptDouble("seconds", 0.0),
                audioBase64: encoded,
                motion: MotionOf(frame),
                last: frame.OptBoolean("last", false));
        }

        /// <summary>`narration` 幀 → <see cref="Narration"/>；沒有 `audio` ⇒ null（呼叫端當「這次沒聲音」）。</summary>
        public static Narration DecodeNarration(JObject frame)
        {
            var encoded = frame.OptString("audio");
            if (encoded.Length == 0) return null;
            return new Narration(
                character: frame.OptString("char"),
                clip: frame.OptString("clip"),
                text: frame.OptString("text"),
                seconds: frame.OptDouble("seconds", 0.0),
                audioBase64: encoded,
                motion: MotionOf(frame));
        }

        /// <summary>`media` 幀 → <see cref="Media"/>；沒有 `url` ⇒ null。</summary>
        public static Media DecodeMedia(JObject frame)
        {
            var url = KotlinText.Trim(frame.OptString("url"));
            if (url.Length == 0) return null;
            return new Media(
                kind: IfBlank(frame.OptString("kind"), "image"),
                url: url,
                name: IfBlank(frame.OptString("name"), "產物"),
                bytes: frame.OptLong("bytes", 0L),
                replay: frame.OptBoolean("replay", false));
        }

        /// <summary>`remind` 幀 → <see cref="Reminder"/>；**用件も言う一句も空** ⇒ null（何も知らせられない一本は出さない）。</summary>
        public static Reminder DecodeReminder(JObject frame)
        {
            var text = KotlinText.Trim(frame.OptString("text"));
            var say = KotlinText.Trim(frame.OptString("say"));
            if (text.Length == 0 && say.Length == 0) return null;
            return new Reminder(
                id: frame.OptString("id"),
                text: text,
                say: say,
                at: frame.OptDouble("at", 0.0),
                clock: frame.OptString("clock"),
                lateMin: frame.OptInt("late_min", 0));
        }

        // ── 內部 ────────────────────────────────────────────────────────────────

        /// <summary>
        /// `motion` 列 → <see cref="Motion"/> 清單：每一列是 `[atMs, shape]`，**壞掉／空的列跳過**
        /// （不是陣列、shape 空、整列是 null／不是陣列都略過）——不讓一列壞掉吃掉整句。
        /// </summary>
        private static List<Motion> MotionOf(JObject frame)
        {
            var motion = new List<Motion>();
            var rows = frame.OptJArray("motion");
            if (rows == null) return motion;
            for (var i = 0; i < rows.Length(); i++)
            {
                var row = rows.OptJArray(i);
                if (row == null) continue;
                var shape = row.OptString(1);
                if (shape.Length == 0) continue;
                motion.Add(new Motion(row.OptInt(0), shape));
            }
            return motion;
        }

        /// <summary>`s.take(n)`：前 n 個字（不足則全部）。</summary>
        private static string Take(string s, int n)
        {
            if (s == null) return null;
            return s.Length <= n ? s : s.Substring(0, n);
        }

        /// <summary>
        /// **`CharSequence.isBlank()` 的 JVM 語意**：空字串、或**每一個**字元都是
        /// <see cref="KotlinText.IsWhitespace"/> ⇒ true。
        ///
        /// ⚠️ **不要用 `string.IsNullOrWhiteSpace`**：那一個把 U+0085（NEL）與 U+180E 當空白
        /// （Kotlin 不當）⇒ 「只有 NEL 的欄位」會被 C# 判成空而**不帶進 payload**，與 Kotlin 分叉。
        /// fixture 有 `chat_nel_kept`／`chat_180e_kept`／`ends_nel_session` 三條釘住這一點。
        /// </summary>
        private static bool IsBlank(string s)
        {
            if (s == null) return true;
            foreach (var ch in s)
            {
                if (!KotlinText.IsWhitespace(ch)) return false;
            }
            return true;
        }

        private static bool IsNotBlank(string s) => !IsBlank(s);

        /// <summary>`ifBlank { fallback }`。</summary>
        private static string IfBlank(string s, string fallback) => IsBlank(s) ? fallback : s;
    }
}
