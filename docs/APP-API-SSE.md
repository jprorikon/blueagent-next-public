# App ⇄ BlueAgent Next：**SSE 契約**（v1 + v2 扩展，2026-10-22 定；2026-10-23 同步）

> **2026-11-08 適用範圍**：手機端的文字／照片聊天、公共区、媒體、貼紙、心情、位置、通話已改走 App Server `/v1/*`
> （見 [`ADR-app-social-platform-20261105.md`](ADR-app-social-platform-20261105.md)）。
> 本契約現在只管 Agent 自己的 `/api/*`：語音簡訊 `/api/audio`、`/api/me`／`/api/state`／`/api/events` 與 MR 端。
> 公共区的 `/api/feed`／`/api/post` 已於 2026-11-08 從 Agent 移除，改直連 App Server `/v1/feed`、`/v1/posts`
> （路由表見 `app/server/board_http.py` 開頭）。

> 這份是 **App 側（Unity 雙端）唯一要照著實作的介面**。核心只有兩種形狀：
> ① 送一輪（回 SSE）；② 開一條常駐 SSE 收推播。其餘是登入／補拿／管理用的普通 JSON GET
> （`/api/me`、`/api/state`、`/api/turns…`、`/api/accounts…`），不改變這兩種資料流。

## 0. 為什麼是 SSE

| 需求 | 為什麼 SSE 是對的 |
|---|---|
| 她邊生成邊說話 | 一輪有多個中間狀態（記憶檢索、思考、逐字、語音、工具、打斷），一次性 JSON 做不到 |
| 語音「先回一句、後補完整答案」 | 補的那條**沒有請求可以回**，只能推；SSE 天生是單向推 |
| 之後要加打字機效果／嘴型 | 每個 `delta`／`speech` 都是獨立事件，App 不必自己切字 |
| Unity 實作成本 | `UnityWebRequest` 的 DownloadHandlerScript 逐段收即可，不必引入 WS 套件 |

WebSocket 被排除的理由：雙向對我們沒用（App 只送「一輪」和「確認」），而 WS 的斷線重連、
背壓、行動網路切換都要自己寫；SSE 是 HTTP/1.1 明文長連，反代（nginx/Caddy）現成支援。

## 1. 端點總表

| 方法 | 路徑 | Content-Type | 用途 |
|---|---|---|---|
| `POST` | `/api/turn` | 請求 `application/json` → 回應 `text/event-stream` | **送一句話**（打字） |
| `POST` | `/api/audio` | 請求 `audio/wav` 或裸 PCM16 → 回應 `text/event-stream` | **送一段語音**（伺服器轉寫） |
| `POST` | `/api/image` | 請求 `image/png`／`jpeg`／`webp` → 回應 `text/event-stream` | **送一張圖**（第一幀 `image`，之後照常走一輪） |
| `GET` | `/api/events` | 回應 `text/event-stream` | **常駐**：收推播（後續答案、主動訊息、主人的心情） |
| `GET` | `/api/health` | `application/json` | 探活（App 啟動時用一次）；公開回應不含 `counts`，帶 owner token 才附運行計數 |
| `GET` | `/api/me` | `application/json` | 登入後「我是誰、哪種端、能做什麼」 |
| `GET` | `/api/state` | `application/json` | 補拿現況（不依賴事件有沒有收到） |
| `GET` | `/api/turns?since=…` | `application/json` | v2 斷線補拿（見文末） |
| `GET` | `/v1/stickers?entity=…` | `application/json` | **App Server** 客户端贴纸目录（见「表情包」） |
| `GET` | `/v1/stickers/<entity>/<id>` | `image/png`／`image/jpeg` | **App Server** 贴纸图片 |
| `GET` | `/api/stickers?entity=…` | `application/json` | 旧 Agent 兼容目录；新客户端不再调用 |
| `GET` | `/api/stickers/<entity>/<id>` | `image/png`／`image/jpeg` | 旧 Agent 兼容图片；供历史 URL 使用 |
| `GET` | `/api/media/<id>` | `image/*` | 相冊／氣泡的圖（只給上傳的那個人） |
| `POST` | `/api/voice/call`／`ping`／`interrupt`／`hangup` | `application/json` | **單人通話**（只給 master，見「單人通話」） |
| `GET` | `/api/diary?entity=&days=` | `application/json` | 她的日記**索引**（只給 master，見「日記」） |
| `GET` | `/api/diary/day?entity=&date=` | `application/json` | 某一天的筆記（點開才拿） |
| `GET` | `/api/diary/work?entity=&name=` | `application/json` | 一件作品全文 |
| `GET` | `/api/mood?entity=` | `application/json` | 她的心情（只給 master；帶與語言無關的 key） |

`/api/turn` 與 `/api/audio` 都是「請求 → 一串事件 → 這一輪結束」。**連線不保持**，結束即關。

### 認證
```
Authorization: Bearer <token>
```
master token 或 guest token（guest 的能力由伺服器決定，App 不要自己判斷權限）。

### 送一輪的請求欄位（`/api/turn`）
```json
{ "text": "おはよう", "entity": "mira", "want_voice": true, "request_id": "uuid-可省略" }
```
| 欄位 | 必填 | 說明 |
|---|---|---|
| `text` | ✅ | 這一句 |
| `entity` | ✕ | 預設 `mira`（一顆 bot／一個 App 端點只代表一個實體） |
| `want_voice` | ✕ | 要她出聲（僅 master 有效；guest 一律不出聲） |
| `request_id` | ✕ | 冪等鍵；重送同一條不會重複回覆 |
| `mode` | ✕ | `chat`／`work`；**master 一律按 work 执行，guest 一律按 chat 执行**（传了也不会改变，见 `agent/server/api/turns.py` 的 `prepare_send`） |
| `group` / `participants` / `ownerless` / `mention` / `addressed_to` | ✕ | 仅 master 生效；群场景/无主调试用 |
| `source` | ✕ | 调用端标记来源（如 `discord:vc`／`mic:…`） |
| `kind` / `media` | ✕ | 图片输入等扩展（通常由 `/api/image` 填） |
| `kind: "sticker"` + `sticker` | ✕ | 送一張表情包（`sticker`＝id；`text` 可空）。她讀到的是「（スタンプ：意思）」；id 不在這個角色那一包 ⇒ 400 `unknown sticker` |

`/api/audio` 的參數走 header：`X-Entity`、`X-Mode`、`X-Lang`、`X-Rate`、`X-Request-Id`、
`X-Want-Voice`、`X-Call`（都是選填；`X-Want-Voice` 只有 master 生效；`X-Call` 見「單人通話」）。

## 2. 事件格式（一輪的那條連線）

每個事件都是 SSE 的 `event:` ＋ `data:`（JSON，UTF-8）：
```
event: frame
data: {"kind":"delta","payload":{"text":"おは"},"turn":"th_...","seq":3}

event: done
data: {"kind":"done","payload":{"text":"おはよう、オーナー。"},"final":true}
```

### `frame` 事件的內容（`data.kind`）
| kind | payload | App 該做什麼 |
|---|---|---|
| `transcript` | `{text, language, probability, duration}` | 只有 `/api/audio` 會有；顯示「我聽到你說了…」 |
| `image` | `{media: {...}}` | 只有 `/api/image` 的第一幀；表示圖片已收下 |
| `memory` | `{recall: [...], hits}` | 可顯示「她想起了什麼」（除錯用，正式介面可藏） |
| `thinking` | `{}` | 顯示思考中 |
| `part` | `{index, text, final}` | **server 已切好的氣泡**（每條最多 3 句，與 TTS 同一個句柄）；照 `index` 顯示，**端末不要再自己分段** |
| `delta` | `{text}` | **逐字追加**到她的對話泡泡 |
| `speech` | `{audio, seconds, voice, seq, duration_ms}` | `audio` 是 base64 wav；按 `seq` 排隊播放 |
| `speech_error` | `{reason}` | 這一句沒聲音（`tts_down`／`floor_busy`），文字照常顯示 |
| `tool` | `{...}` | 她在做別的事（委任進度等），可顯示「正在處理」 |
| `sticker` | `{id, entity, label, url}` | 她回了一張表情包：畫 `url` 那張圖（只在 App 的聊天回合出現） |
| `motion` | `{id, tag, url, hash, seconds, entity}` | 她的動作（後端 Kimodo，2026-10-31）：3D 模式且 `entity` 是舞台上的角色才播；片段用 `GET url` 取（`bamotion/1`，ETag＝`hash`）。規格見 `docs/PLAN-motion-kimodo-20261031.md` §4–§5 |
| `call` | `{id, state, end, …}` | 通話狀態變了；`state: "ended"`＝她自己掛了電話 |
| `interrupted` | `{at, reason}` | 被打斷（不是錯誤）；停止打字機、清掉半句 |
| `done` | `{text}` | 這一輪結束；`text` 是全文（用來校正逐字結果） |
| `error` | `{error}` | 失敗；照錯誤碼處理，不要重送超過一次 |

> **排序保證**：同一條連線內事件有序；`seq` 只在 `speech` 上保證單調（播放順序）。
> **`done`（`final: true`）是語意上的結束**；之後還會有一條 `result`
> （`{turn, following_up, observed…}` 元資料），然後連線關閉。App 收到 `done` 就可以收尾 UI，
> `result` 只拿來存 thread id／判斷有沒有後續。

## 3. 常駐推播（`GET /api/events`）

```
event: ready
data: {"principal":"owner"}

: ping                       ← 每 15 秒（沒有內容，保持連線活著）

event: delivery
data: {"kind":"delivery","text":"資料整理好了：…","entity":"mira","thread":"th_...","at":...}

event: mood
data: {"kind":"mood","entity":"mira","end":"phone","mood":{"key":"happy","valence":0.4,"arousal":0.2,"social_need":0.0,"revision":7},"at":...}
```

App 收到 `delivery` 的處理方式：**當成她主動說的新一句**（新的泡泡＋若帶語音就播放）。
`mood` 是主人的情緒快照，只更新同一 `entity` 的徽章，**不能進聊天泡泡或 outbox**；guest 不會收到。
appraisal、主人互動（寂寞度重置）與心跳衰減都會觸發；相同讀數去重。首次進頁、回前景與重連仍以
`GET /api/mood?entity=…` 對帳，App 每 120 秒的低頻 GET 只作兜底。

### 按端分流（2026-11-01：MR 頭顯自己一條管道）
請求帶 `X-Source: mr`（或 `GET /api/events?end=mr`）＝Quest 3 的 MR 端；不帶／`phone`＝手機。
`delivery` **照來源送回那一端**：`mr:` 開頭的只給 MR；其他（含主動訊息）只給手機。重播（`Last-Event-ID`）也照這條。

### 什麼時候會有 `delivery`
- 語音回合走「先短回一句、真正的工作排隊做」⇒ 短回覆在那條請求的 SSE 裡，完整答案在 `delivery`
- 之後的主動訊息（提醒、LifeLoop 主動開口）也會走同一條

### 斷線與重連
- **v2 起不再「不重播」**：`/api/events` 帶 `Last-Event-ID`（或 `?since=`）補洞；每輪也會落
  `data/outbox/`，用 `GET /api/turns?since=…` 補回答（詳見文末「契約 v2」）。
- 重連策略：指數退避 1s → 2s → 4s，上限 30s；`ready` 事件代表連上了。
- 補拿現況仍以 `GET /api/state` 為準（它是最終真相，不依賴事件有沒有收到）。

## 4. 語音回合的特例（重要）

`/api/audio`（master 的回合；服务端一律按 `work` 执行；guest 的语音回合是 `chat`）：

```
event: transcript   ← 我聽到什麼
event: delta …      ← 短回覆（沒有工具、沒有委任，只求馬上出聲）
event: speech       ← 短回覆的聲音
event: result       ← {"following_up": true}
（連線結束）

…幾十秒後，在 /api/events 那條線上…
event: delivery     ← 真正的答案（做完的事）
```

App 端要點：**`result.following_up=true` 時不要關掉 `delivery` 的訂閱、也不要把對話標成「已結束」**。

## 4.5 單人通話（2026-10-29，只給 master）

一通電話＝伺服器上一個 `end=phone`（MR 端 `X-Source: mr` 撥的是 `end=mr`）的 VoiceSession。通話期間她的回覆**一律出聲**（粘性，不必帶 `want_voice`）。
一個角色同時只有一通：**後撥的那一端蓋掉先撥的**（手機 ⇄ MR，owner 2026-11-01）；被蓋掉的那一端下一次心跳（或送一句）拿 `410 {"error":"taken_over"}` ⇒ 退出通話並明示「被其他終端接管」（不點名設備）。其他原因沒了的一通（空閒超時、她掛、主人掛）照舊 `410 call_ended`。

| 步驟 | 請求 | 回應 |
|---|---|---|
| 撥 | `POST /api/voice/call {"entity"}` | `200 {session:{id,…}}`；同一端已經在通 ⇒ 回同一通（`existing:true`）；另一端在通 ⇒ 掛掉那一通、撥這一端的；她在 Discord 語音房 ⇒ `409 busy_elsewhere` |
| 說一句 | `POST /api/audio` ＋ `X-Call: <id>`、`X-Entity` | 照常的 SSE（`transcript` → `accepted` → `delta`／`speech` → `done`）；**不拆**「先短回、後交付」；來源記成 `phone:call`（MR：`mr:call`；實時源，可搶佔）。MR 的回合不進手機的聊天紀錄，掛斷也不留通話標記 |
| 打斷 | `POST /api/voice/interrupt {"entity"}` | 這之前開始的合成全部作廢（端末自己停播、丟掉舊那一輪的幀） |
| 心跳 | `POST /api/voice/ping {"entity","call_id"}` | 每 15 秒；那一通沒了 ⇒ `410 call_ended`。超過 `BLUEAGENT_CALL_IDLE_S`（預設 45 秒）**沒有心跳**（端末掉線，不是「你沒說話」），伺服器自己掛 |
| 掛 | `POST /api/voice/hangup {"entity"}` | 放掉發言權 |

`X-Call` 只認主人（guest ⇒ 403）、只認這個角色現在那一通（掛了／換了 ⇒ 410）。
通話中的回合 prompt 帶 `[[call]]`：她會講短、講口語。斷句在端末（`CallVad`），伺服器只吃「一句一個音檔」。

**沒講完先扣住**（2026-10-30）：伺服器轉寫時順便判「這句講完了沒」（本地 qwen，和糾錯並行）。
判成沒講完 ⇒ 這句先不起回合，最多等 `BLUEAGENT_CALL_HOLD_S`（預設 1.5 秒）；這期間主人接著講的下一句
會把它併進去（下一句的 `transcript.text` 是兩句合起來的）。被併走的那條線回 `transcript` → `merged{call}`
就結束，沒有 `accepted`／`done`——端末這時通常已經因為主人又開口而放掉它了，忽略即可。

## 4.6 表情包（2026-10-29；C-17 于 2026-11-07 拆分）

素材唯一真源是客户端 `app/client/assets/stickers/<entity>/`＋`_shared/`，由 App Server 镜像携带；
Agent 镜像不含素材包。可选清单 `stickers.json`（`{"stickers":[{"id","file","label","meaning"}]}`），
`meaning` 是 Agent 选图／读图的唯一语义依据。只收 PNG／JPEG，换素材不必重开服务或重出客户端。

- Unity 读 `GET /v1/stickers?entity=<entity>`，返回 `{entity,version,stickers:[{id,entity,label,meaning,url,hash}]}`；
  图片走 `GET /v1/stickers/<entity>/<id>`。两条都要求平台 token，Agent 只能读自己的目录，其他账号受可见域限制。
- Agent 用自己的 App Server token 调 MCP `sticker.list {"entity":"<id>"}`，只消费 ID 与 meaning；
  网络失败时使用短期旧缓存，没有缓存则不提供贴纸工具，不阻塞聊天。
- 旧 `/api/stickers` 目录和图片端点保留给历史消息；远程模式下 Agent 只在旧图片 URL 被请求时下载并缓存那一张，
  不预装整包，API 永不暴露服务器文件路径。
- 主人送：`POST /api/turn {"kind":"sticker","sticker":"<id>"}`；outbox 那一列多 `asked_sticker`。
- 她回：工具 `sticker.send` ⇒ `sticker` 帧；outbox 那一列多 `stickers: [{id, entity, url}]`。
  只在 App 的聊天回合给她这支工具（Discord／通话中看不到图）。

## 4.6.1 动作片段（2026-10-31；C-17 保持运行契约）

`GET /api/motion/<id>` ⇒ `bamotion/1` JSON（SOMA 骨架、30 fps、相对 T-pose 的世界旋转＋Hips 位置）；`ETag: "<id>"`，304 同下节。
一轮最多一条 `motion` 帧，时机在她开口时。服务没起／太慢 ⇒ 不发，聊天照常。
清单：`GET /api/motion?entity=<entity>` ⇒ `{count, tags{<tag>: [<id>…]}}`。格式与库实现归
`app/client/assets/motion.py`，旧 import 仅兼容转发；实际片段仍在 `$BLUEAGENT_DATA_DIR/motion`，
旧 URL、Unity／MR 播放与 `Resources/models3d` 均不变。完整规格：`docs/PLAN-motion-kimodo-20261031.md`。

## 4.7 App 侧缓存与哈希（2026-10-29）

owner：「图片、表情包和聊天记录每次都要重新下载 ⇒ 采用 app 侧缓存，对比线上的哈希决定要不要重新同步」。

哈希＝sha256 的前 16 个十六进制字。服务器在这几条上回 `ETag: "<哈希>"`
＋ `Cache-Control: private, no-cache`，请求带 `If-None-Match: "<哈希>"` 且没变 ⇒ **304、没有本文**：

| 路径 | ETag 是什么 | 回应里也看得到 |
|---|---|---|
| `GET /api/turns?…` | 这一窗 `turns` 的哈希（`stats` 不算） | `digest` |
| `GET /v1/stickers?entity=`（旧 `/api` 同契约） | 整份清单的哈希 | `version`；每张另有 `hash`（＝那张图的 ETag） |
| `GET /v1/stickers/<entity>/<id>`（旧 `/api` 同契约） | 图档内容的哈希 | — |
| `GET /v1/media/<id>` | 图档内容的哈希 | — |
| `GET /api/diary?…` | 索引内容的哈希 | `digest` |
| `GET /api/diary/day?…` | 那一天内容的哈希 | `digest` |

304 不松动权限：没 token 照样 401、不可见角色或别人的图照样 404。

端末（Unity `SyncCache`，放 `temporaryCachePath/next-sync/<服务器＋身份的哈希>/`，上限 200 MB、超过丢最久没用的）：
- 聊天记录、表情包清单：先铺本机那份，再带哈希来问；304 就结束，200 就存新的、补画没画过的；
- 表情包的图：清单的 `hash` 跟本机那份一样 ⇒ **不发请求**；气泡里的（没有 `hash`）有就直接用；
- 相册的图：id 每次上传都是新的、内容不会变 ⇒ 本机有就直接用；
- 其余情况下载时都带本机那份的哈希（`If-None-Match`）。

## 4.8 传输瘦身（2026-10-30）

- **缩图**：`GET /v1/media/<id>?w=360`（相册墙）、`GET /v1/stickers/<entity>/<id>?w=192`（表情盘）。
  只收这两个宽，别的宽当原图。App Server 用 ffmpeg 缩、放 `$BLUEAGENT_APP_DATA/thumbs/`（原档换了自动重做）；
  PNG 留透明，其余出 JPEG。**ETag＝`<原图哈希>-w<宽>`**——端末用清单里的 `hash` 就推得出来，照样不必问。
  没有 ffmpeg ⇒ 给原图，ETag 照缩图那样标。权限与 304 跟原图一样。
- **語音**：送給 App 的 `speech` 幀與 `GET /api/turns/<id>/audio` 降到 `BLUEAGENT_APP_AUDIO_RATE`（預設 16000 Hz，
  0＝不降；只降不升）。Discord 語音房與 outbox 存檔用原檔。
- **上傳的圖**：`POST /api/image` 的 `Content-Type` 照實際格式（端末看檔頭判斷 JPEG／PNG／WebP）。
- **常駐推播**：端末 45 秒收不到任何位元組（伺服器 15 秒一次 `: ping`）就自己斷、重連；連上後退避歸零。

## 4.9 日記（2026-10-31，只給 master）

owner：「現在讀日記的頁面會載入卡住，然後整個應用卡死」。根因是**資料量**：生活迴圈一天寫 373–1088 步
筆記（實測單日 JSON 4.6–8 MB、七千多張卡片）。現在**索引與單日分兩支**，App 點開那天才拿：

| 路徑 | 回什麼 | 實測大小 |
|---|---|---|
| `GET /api/diary?entity=&days=14` | 每天的日記正文＋`notes_total`（那天有幾步）＋`works`（成品清單，≤50） | **19 KB** |
| `GET /api/diary/day?entity=&date=YYYY-MM-DD` | 那一天的筆記：**最近 30 步**、正文截到 **280 字**、`private_hidden` | **25 KB** |

- 兩支都帶 `digest` ⇒ 可以帶 `If-None-Match` 問 304（App 先鋪本機快取再問）。
- `〔private〕` 段落一律不給，索引只交代「藏了幾段」；日期要真的是日期（`2026-13-99` 擋掉）。
- App 端（`NextDiaryView`）：索引一份、每一天各一份 `SyncCache`；卡片上「記録 N 件を見る」
  點開才抓那一天、再點一次收起來。**每次點開都先鋪快取、再帶雜湊問一次**（今天的筆記每幾分鐘就多一步，
  只信快取會停在第一次打開那一版）。成品用 `GET /api/diary/work` 單獨開。
- ⚠️ **不相容變更**：`/api/diary` 的每一天不再帶 `notes` 陣列（改成 `notes_total`）。
  舊版 App 打開日記頁會看不到筆記 ⇒ 伺服器和 App 要一起發版。
- 成品清單照**修改時間**新的在前；同一天重做同一步會覆蓋同一個檔，所以舊筆記的〔作品〕連結開到的是最新版。

## 5. 錯誤碼

| HTTP | body | App 行為 |
|---|---|---|
| 400 | `{"error":"text required"}` | 不要重送，這是 App 的 bug |
| 401 | `{"error":"unauthorized"}` | 重新登入／檢查 token |
| 403 | `{"error":"forbidden"}` / `reset_disabled` | 提示「沒有權限」 |
| 409 | `{"error":"busy_elsewhere"}` | 通話：她正在別處講電話 |
| 410 | `{"error":"call_ended"}` | 通話已經掛了：收線 |
| 413 | `{"error":"audio too large"}` | 音檔上限 12 MB |
| 422 | `{"error":"stt_failed"}` | 語音轉不出字：提示「再說一次」 |
| 429 | `{"error":"rate_limited"}` | 退避重試 |
| 503 | `{"error":"stt_unavailable"}` | 伺服器沒開聽：顯示「聽不到」 |

## 6. 版本與相容

- 契約版本寫在 `GET /api/health` 的 `api` 欄位（目前仍是 `{"api":"sse-v1"}`；v2 是**向後兼容的增量**，尚未單獨升版）。
- **只增不改**：新增 `kind` 必須讓未知 `kind` 被 App 忽略（App 請照這個寫）。
- 欄位不刪、不改名；要淘汰先標 `deprecated` 一個版本。

## 契約 v2：斷線不丟交付（2026-10-22）

**問題**：一輪的回覆只走 `POST /api/turn` 的 SSE 回流。App 半路斷線（鎖屏、切網路、地鐵）
之後的 `delta` / `speech` / `done` 全部沒收到；`/api/events` 的 `Last-Event-ID` 只補
「主動推送」那條，**補不回這一輪的回答**。

**改了什麼**

1. `POST /api/turn` 的第一幀固定是：
   ```
   event: accepted
   data: {"kind":"accepted","payload":{"turn_id":"t1792656165094-0001"}}
   ```
   端末**一定要記住 `turn_id`**（最後一個「完整收到 done」的 id 就是重連游標）。
2. 每一輪結束（成功或失敗）都會落一筆到 `data/outbox/index.jsonl`；
   有聲音的回合另存 `data/outbox/<turn_id>.wav`（多句合併）。
   保留最近 `BLUEAGENT_OUTBOX_KEEP`（預設 400）輪、音檔總量 `BLUEAGENT_OUTBOX_AUDIO_MB`（預設 96 MB），
   超量時**先丟最舊的音檔、文字留著**。
3. 重連補拿：
   | 端點 | 用途 |
   |---|---|
   | `GET /api/turns?since=<turn_id>&limit=50` | 回「那個 id 之後」的回合（含 `text`／`audio`／`error`） |
   | `GET /api/turns?entity=<id>&since=<turn_id>` | 同上但**只在該角色的回合裡**找游標（端末切角色後的補拿；`since` 與 `entity` 可並存） |
   | `GET /api/turns?entity=<id>&source=phone` | 只回**這一端**（手機 App）的回合：App 的「對話紀錄」用它，Discord 上說過的話不進來（owner 2026-10-27 定；`source` 是前綴比對，`phone` 命中 `phone:owner`／`phone:guest-…`） |
   | `GET /api/turns/<turn_id>` | 單筆完整紀錄 |
   | `GET /api/turns/<turn_id>/audio` | 合併後的 wav（`audio/wav`） |

   `since` 給一個找不到的 id ⇒ 回最近 limit 筆（讓端末自己對齊，不要空白）。
4. **`parts`：一輪回覆的分段**（owner 2026-11-04）。串流時她是「一條一條短訊息」（`part` 幀，
   server 用 `runtime/reply_split.py` 切句），補拿／歷史如果只畫 `text` 就會變成一大條 ⇒
   outbox 每列多存 `parts: ["…", "…"]`（**>1 條才寫**；單條、舊列、主動訊息沒有這個鍵）。
   端末規則：有 `parts` 就一條一個氣泡，沒有就照舊把 `text` 畫成一條。`text` 仍是全文。
5. **ACL**：三個端點都只回 `principal` 自己的回合（主人與 guest 不互通）。

**端末流程（建議）**

```
收到 accepted → 記下 turn_id（pending）
收到 done     → 把文字／音檔落地，pending 清掉，last_good = turn_id
斷線重連      → GET /api/turns?since=last_good
               → 逐筆補：text 直接顯示；audio:true 的再 GET .../audio
```

**驗收（真模型＋真 TTS，容器內）**

```
斷線 turn_id: t1792656165094-0001        （只讀第一幀就 close）
補回內容: text='うん、聞こえてるよ、オーナー。ちゃんと繋がってる。' speech=2 audio=True bytes=304684
GET /api/turns        -> 200 筆數 1
GET /api/turns/<id>   -> 200 同上
GET .../audio         -> 200 304684 bytes
```
