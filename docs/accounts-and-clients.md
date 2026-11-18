# 帳號層與兩種端（master／guest）— 給 Unity 改造用

> owner 2026-10-22：「不需要為主人和 guest 準備帳號層麼？我的計劃是準備完帳號層就可以開始
> 基於現有的 unity repo，做 master 端和 guest 端改造了」。
> 這份是**端末要看的契約**；伺服器側實作在 `agent/brain/runtime/identity/accounts.py`。

## 1. 帳號模型

| | master | guest |
|---|---|---|
| 數量 | 一個 runtime **只有一個**（`acct_owner`） | 多個 |
| 來源 | 啟動時由既有 owner token 自動收編（`data/auth.json` → `data/accounts.json`） | 主人在管理面簽發，或既有的 guest 核可流程自動匯入 |
| 能力 | `chat, admin, manage_accounts, manage_guests, delegate` | `chat`（可再加） |
| 能看到的角色 | 全部 | 自己綁定的那一個（沒綁定就給第一個，至少能聊） |

- 账号层签发的 token 形如 `ba_<48 hex>`；GuestStore 核可流程仍发 `gt_…`；主人旧 token `ot_…` 仍有效。三者都收编进同一张账号表。
- **回合模式由发起角色固定**（2026-10-23 定）：master 发起一律 `work`，guest 发起一律 `chat`；端末传 `mode` 不会改变这个结果。
- guest 可以发**语音输入**，但**输出声音一律关闭**（`want_voice` 只对 master 生效）；`can.voice` 表示端末可用语音输入，不代表 guest 会出声。
- **文件只存 SHA-256**（`data/accounts.json`，0660）：备份被看到不等于 token 泄漏。
  0660 是刻意的：容器以 root 写，主机侧工具（gid 980）要读得到；能读的人也只拿到哈希，不是 token。
- 撤销／轮替**立刻生效**（`AuthService.authenticate` 先问账号层）。

## 2. 端末啟動流程（兩端相同，只有 UI 不同）

```
① 讀本機存的 token（第一次由使用者貼上）
② GET /api/me          ← 一切都從這裡開始
③ 依回應決定 UI：
     role == "master"  → master 端（顯示管理面、帳號管理、白名單、活動控制）
     role == "guest"   → guest 端（只顯示聊天／語音；隱藏所有管理入口）
④ 之後所有請求都帶 Authorization: Bearer <token>
```

`GET /api/me` 回應（**永遠不含 token／雜湊**）：
```json
{
  "account_id": "acct_owner",
  "role": "master",
  "principal": "owner",
  "display": "owner",
  "capabilities": ["admin", "chat", "delegate", "manage_accounts", "manage_guests"],
  "entities": [{"id": "mira", "name": "ミラ"}, ...],
  "can": {"chat": true, "voice": true, "trpg": true,
          "admin": true, "manage_accounts": true, "manage_guests": true, "delegate": true},
  "created_at": 1792656350.85,
  "device": "primary"
}
```

## 3. 管理面（只給 master）

| 端點 | 用途 |
|---|---|
| `GET /api/accounts` | 列出帳號（含已撤銷；不含 token） |
| `POST /api/accounts` `{principal, display}` | 簽一個 guest；**明文 token 只回這一次**（`role:"service"` 簽 bot 用的服務帳號，見 §3.0.1） |
| `POST /api/accounts/<id>/rotate` | 換 token（舊的立刻失效），回新的明文 |
| `POST /api/accounts/<id>/revoke` | 撤銷（該 token 之後一律 401） |

其餘既有管理面照舊（`/api/owner/*`、`/api/guests/*`、`/api/activity/*`、`/api/voice/*`）——
guest 打這些一律 **403 `{"error":"forbidden"}`**。

她的內心與記憶也**只給主人**（owner 2026-11-02「做掉」）：

| 端點 | 給 guest | 說明 |
|---|---|---|
| `GET /api/memory/windows`、`GET /api/memory/trace` | 403 | 窗口摘要＝她的理解、事實履歷＝記憶怎麼被改寫 |
| `GET /api/self/next` | 403 | AEM 的自主任務決策 |
| `GET /api/mood`、`GET /api/diary*` | 403 | 心情流水、日記與成品 |
| `GET /api/state` | **200** | 端末狀態頁兩端都用；guest 拿的是**自己 principal** 的快照，`affect`／`heartbeat` 只給主人 |

> 2026-11-02 之前上面三支（memory×2、self/next）只檢查「有沒有登入」，App 沒有入口但 API 開著；
> 守門在 `agent/tests/test_guest_permissions.py`。

## 3.0 多台端末（owner 2026-11-16；主人與客人都可以）

一個帳號可以有多台端末，每台一把自己的 token（只存雜湊）；原本那把＝`primary`（「最初の端末」）。
`/api/me` 多回一欄 `device`（這次請求是哪一台）。

| 端點 | 誰 | 用途 |
|---|---|---|
| `POST /api/pair/start` `{base_url}` | 已登入（主人／客人） | 開配對碼 ⇒ `{code, expires_at, url, qr}`：6 位數字、5 分鐘、一次限り；同帳號再開一次舊碼作廢。`url`＝`<base_url>/pair/<碼>`，`qr`＝它的二維碼 PNG（data URI；伺服器沒裝 `segno` 時為空） |
| `POST /api/pair/claim` `{code, name}` | **不用登入** | 新端末換 token ⇒ `{token, account, device}`（明文只回這一次）；錯碼／過期 404 `bad_or_expired_code`；每 IP 每分鐘 10 次，超過 429 |
| `GET /api/devices` | 已登入 | 自己帳號的端末：`device_id／name／created_at／last_seen_at／revoked／current` |
| `POST /api/devices/revoke` `{device_id}` | 已登入 | 撤銷自己帳號的一台（`primary` 也可以，撤了之後連 `auth.json` 的舊路徑也進不來；`/api/accounts/<id>/rotate` 會簽出新的 primary） |
| `GET /pair/<碼>` | 公開 | 系統相機掃二維碼開到的落地頁；一顆鈕跳 `blueagent://pair?code=<碼>`（App 的 intent-filter 收），不消耗碼 |

App Server（`/v1/*`）的人類 token 以 Agent 為準：它用 kick token 問 `POST /api/app/introspect`（只送 SHA-256），
答案快取 60 秒。⇒ 新端末馬上能用 `/v1/*`；已經開著的常駐 SSE 每 30 秒重驗一次 token，驗不過就收線。

**Agent 問不到時**（審核 R1，owner 2026-11-17 定；`app/server/device_auth.py`）：不再退回靜態帳號表，改看這把 token **上一次的結論**——
上次「無效」⇒ 拒；上次「有效」且距今 ≤ `BLUEAGENT_APP_AUTH_GRACE_S`（預設 600 秒）⇒ 照舊放行；沒確認過或已超過 ⇒ 拒。
角色帳號不受影響（只看本地表）。沒設定 kick token（introspection 沒開）時才只看靜態表。

**撤銷的生效上限**：Agent 在線 ≤ 60 秒（SSE 再加 30 秒重驗，約 90 秒）；Agent 離線 ≤ 距上次確認 600 秒（SSE 約 630 秒）。
代價：App Server 重啟時 Agent 也不在 ⇒ 人類帳號要等 Agent 回來才進得了 `/v1/*`（結論只放記憶體）。

## 3.0.1 服務帳號（每顆 bot 一把，owner 2026-11-16）

`POST /api/accounts` `{"role":"service","source":"discord","entity":"<角色>"}`（只給主人）簽一把，明文只回一次；
principal 固定是 `svc-<源>-<角色>`，不登記成人、不能配對。

- **只有服務帳號**的 token 會讀 `X-Source`／`X-External-Id`：`X-Source` 要等於它綁的源、外部 id 要已綁定，
  否則 403 `unknown_external_id`；`entity` 不是它的角色 ⇒ 403 `entity_not_bound`。
- 主人／客人的 token 帶這兩個頭**一律忽略**（以前主人 token 可以藉此冒充任一 Discord 用戶）。
- 只能打 `/api/turn`、`/api/stream`、`/api/image`、`/api/media`、`/api/audio`，其他路徑一律 401。
- 現役 Discord bot 跑在 Agent 進程內、不經 HTTP，不需要這把；給拆到進程外的 bot 用。

## 3.1 guest 可以送什麼

| 動作 | guest | 備註 |
|---|---|---|
| 打字 | ✅ | 模式一律 `chat` |
| 語音訊息（`/api/audio`） | ✅ | 她**不會出聲**（`want_voice` 只對主人生效） |
| 送照片（`/api/image`） | ✅ | 2026-11-02 修好：從前用一個不存在的 `auth.can_send` 判 ⇒ 恆真 ⇒ guest 一律 403 |
| 貼紙（`kind=sticker`） | ✅ | 送的是那張的「意思」，她看不到圖 |
| 五子棋（`/api/game`） | ✅ | 按 principal 各開各的局 |
| **通話**（`/api/voice/*`、`/api/audio` 帶 `X-Call`） | ❌ 403 | 通話只有主人；`X-Call` 的閘在轉寫之前 |

TRPG 也只給 master（owner 2026-10-29「不要讓 guest 控制 agent 加入 trpg」）：
`GET /api/trpg/tables|table`、`POST /api/trpg/enter|say|act` 對 guest 一律 403
（看桌也是拿她的 hub 帳號去問的），`/api/me` 的 `can.trpg` 對 guest 是 `false`，
App 的「+」選單也不給 guest 這一項。

## 4. 錯誤語義（端末要分清楚）

| 狀態 | 意思 | 端末動作 |
|---|---|---|
| `401 unauthorized` | 沒有/壞 token、或帳號被撤銷 | 清掉本機 token，回登入頁 |
| `403 forbidden` | token 有效但這個帳號不能做這件事 | 不要回登入頁；隱藏該功能 |
| `404 turn_not_found` | 那一輪不在 outbox（超過保留期） | 當作遺失，不要重試 |

## 5. 斷線補拿（契約 v2，見 `docs/APP-API-SSE.md`）

`POST /api/turn` 第一幀 `accepted{turn_id}`；重連用 `GET /api/turns?since=<last_good>`
把漏掉的回覆補回來（文字 + wav）。

## 6. Unity 改造的最小步驟（建議）

1. **登入頁**：一個輸入框貼 token → `GET /api/me` → token 存系統安全儲存（Android Keystore／iOS 鑰匙圈，`NextConfig.Token`；用不了才退回 `PlayerPrefs`），master 端另存「上次的 entity」（`PlayerPrefs`）；
   另一欄輸入 6 位配對碼 → `POST /api/pair/claim` 換 token，再走同一條 `/api/me`（§3.0）。
2. **主選單分流**：`role` 決定載入哪個 prefab 組；`can.admin=false` 時**整個管理選單不要 instantiate**。
3. **通訊層**：一個 `ApiClient` 帶 token；401 → 回登入頁，403 → 只顯示提示。
4. **guest 端**：只留聊天／語音；`entities` 給全部角色，聊天頁跟 master 一樣可以選說話對象
   （owner 2026-10-29；先前只給一個）。綁定在某個角色上的 guest 帳號（principal＝角色 id）仍只給那一位。
5. **master 端**：沿用既有畫面，多一個「帳號」頁（列表／簽發／輪替／撤銷）。

## 7. 已知邊界（先講清楚，免得端末做錯）

- **一個 bot ＝ 一個實體**（owner 2026-10-22 定案）：Discord 上不會因為 guest 多就長出分身。
- guest 帳號與 Discord 白名單是**兩件事**：白名單管「在 Discord 裡誰能跟她說話」，
  帳號管「哪個端、用什麼 token」。兩者用 `principal` 對得上（`guest-<discord_id>`）。
- 主人 token 明文只在「第一次啟動」或「輪替當下」出現一次；之後檔案裡只有雜湊。
