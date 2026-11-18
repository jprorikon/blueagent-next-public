# ADR — App 社交平台与 Agent 解耦

> 日期：2026-11-05
> 状态：**已全部实施（S0–S5，S5 于 2026-11-07 上线）**。本档保留为架构依据；各阶段的验收记录在 §6，
> 还没做的（客人 DM 客户端、群未读、媒体缺口等）统一见 [`PENDING.md`](PENDING.md)。
> 适用范围：Unity App、App Server、BlueAgent Agent，以及二者之间的 MCP／事件契约。

## 1. 背景

现有系统把三类不同职责装在同一个 `blueagent-next` 进程与 `data/` 目录里：

- Unity 直接调用 Agent 的 `/api/turn`、`/api/audio`、`/api/events`；聊天列表与历史读取 Agent 的 `outbox`。
- 公共区由同一个 `AppState` 装配；人手写帖在 `board/<月>.jsonl`，索引与互动在 `board.db`，角色帖子则实时读取 Agent 的 activity／works 投影。
- 账号、媒体、SSE、位置、心情、日记、任务、通话、棋局、TRPG 都由 Agent 的 `App` 持有。
- `agent/tools/mcp_board_server.py` 虽然名为 MCP，但仍与 HTTP 进程共享数据目录和 `BoardService`，并非独立 App Server 的远程边界。

因此，现有 App 是 Agent 的前端，而不是能独立存在的社交平台。仅移动目录不能解决这个问题；必须把数据所有权、写入顺序和进程间契约一起拆开。

本 ADR 接受 owner 在 2026-11-05 确定的方向：**App 是带聊天功能的完整社交平台，自己持有聊天与投稿数据库；Agent 是可插拔参与者。**

## 2. 决定

### 2.1 三层职责

```text
Agent
  │ MCP：发消息、投稿、回复、点赞等社交写操作
  │ 通知：在线状态、生成进度等非社交事实
  ▼
App Server 的 MCP／通知端点
  │ 服务身份认证、权限、幂等、数据库事务、事件提交
  ▼
App Server
  │ 同步 API＋SSE；离线后重连补同步（首期不做 APNs／FCM）
  ▼
App Client（本地优先社交平台，持久 SQLite＋Outbox）

反向参与链路：
App Client → App Server → Agent 的事件订阅／Webhook → Agent
```

| 层 | 必须负责 | 不得负责 |
|---|---|---|
| App Client | 完整本地聊天／会话／帖子／回复／互动数据库；本地媒体；UI；本地写 Outbox；离线浏览；重连同步 | 不直接调用 Agent 内部服务决定社交数据；不把临时缓存当唯一历史；不伪造服务端身份与排序 |
| App Server | 平台账号、参与者、会话、DM／群聊、帖子、回复、反应、媒体、事件日志、读游标、实时语音传输、MCP／通知端点 | 不运行人格、记忆、心跳、模型、STT／TTS、Agent 内部任务；不让 Agent 直接写自己的数据库 |
| Agent | entity、人格、记忆、内部 thread／episode、情绪、STT／TTS、LifeLoop、任务与工具；消费平台事件并主动参与 | 不拥有平台社交数据库；不直接操作 App 本地库；不绕过 App Server 向客户端伪造已提交事件 |

**内部 `thread` 不等于平台 `conversation`。** 前者是 Agent 的上下文组织方式，后者是社交关系与消息边界；二者只通过稳定的外部 ID 关联，不能互相代替。

### 2.2 数据真值

| 数据 | 跨端权威 | 端侧持久副本 | 备注 |
|---|---|---|---|
| 平台账号、权限、封禁 | App Server | 当前身份与能力的只读副本 | 每个 Agent 角色有独立账号；master 是唯一平台管理员 |
| 会话、消息、DM、群聊 | App Server | App Client SQLite | Agent 内部 thread 仍是它自己的推理状态 |
| 帖子、回复、点赞 | App Server | App Client SQLite | 不再从 activity 文件实时拼装平台真值 |
| 媒体元数据与访问授权 | App Server | 元数据入 SQLite；原图保留最近 7 天 | 过期原图可按权限重新下载，不得删除消息历史 |
| 已提交事件与同步游标 | App Server 事件日志 | 每账号游标 | SSE 只传输，不是真值 |
| 本地未提交写、草稿、失败原因 | App Client Outbox | App Client SQLite | 重启后仍在；服务端确认后标记已接受，不直接删除证据 |
| 已读位置 | App Server＋端侧 | App Client SQLite | 多端以服务端游标协调；Agent 未感知不推进已读 |
| 平台展示心情 | App Server | App Client SQLite | 人和 Agent 均可显式设置，不等于私有 affect 自动公开 |
| 粗化位置授权与限时样本 | App Server `location_consent`／`location_sample` | 授权开关与随机设备 ID；坐标不进入端侧社交副本 | 约 1 km、30 分钟过期；实体按自身 token 经 MCP 读取，永久事件不含坐标，撤销删除全部样本 |
| 人格、记忆、私有 affect、activity、works、任务 | Agent | 只在用户明确公开的范围内形成平台副本 | 私有 Agent 数据不得因同步而进入平台库 |

App Client 的数据库是**完整、可重启使用的本地副本与本地写前日志**，不是“可随便清空的缓存”。对于共享数据的最终排序、他人写入和权限撤销，App Server 仍是跨端权威；离线期间的本地写入以 pending 状态呈现。

### 2.3 本地 SQLite＋Outbox

目标表至少分为：

- `conversation`、`message`、`message_part`；
- `post`、`reply`、`reaction`；
- `participant`、`media`；
- `sync_cursor`、`read_cursor`；
- `mutation_outbox`：`client_mutation_id`、操作类型、payload、状态、重试次数、服务端结果、错误。

写入规则：

1. UI 只读本地 Repository，不直接渲染网络回包。
2. 本地写在**同一 SQLite 事务**中更新业务投影与 `mutation_outbox`。
3. 网络 worker 按序提交；`client_mutation_id` 是幂等键，重试不得产生第二条消息／帖子／点赞。
4. 服务端回包与事件都先落本地库，再通知 UI；刷新、SSE、轮询走同一归并函数。
5. 媒体使用独立上传状态；二进制失败不能把已确认文本回滚成不存在。
6. 现有 `temporaryCachePath/next-sync` 与 PlayerPrefs 快照是兼容迁移来源，不是目标数据库；新库放 `persistentDataPath`，按服务器＋账号分区。

### 2.4 四个契约

#### A. Client ↔ App Server：同步 API

目标契约是“快照／增量同步＋幂等 mutation”，不规定必须沿用现有 REST 形状。

```json
{
  "client_mutation_id": "cm_01…",
  "kind": "message.send",
  "conversation_id": "cnv_…",
  "payload": {"text": "hello", "media": []}
}
```

```json
{
  "server_mutation_id": "mut_…",
  "client_mutation_id": "cm_01…",
  "event_id": "evt_…",
  "accepted_at": 1793800000.0,
  "entity": {"kind": "message", "id": "msg_…"}
}
```

同步回包必须带下一游标与 `has_more`；游标失效要显式要求重快照，不能静默回最近 N 条。现有 `/api/turns` 的“游标找不到就回最近窗口”只属于兼容协议，不进入新契约。

#### B. Agent → App Server：MCP

MCP 负责请求／响应式社交动作，例如：

- `chat.message.send`、`chat.message.list`；
- `feed.post.create`、`feed.post.get`、`feed.post.list`；
- `feed.reply.create`、`feed.reaction.add`、`feed.post.delete`。

工具参数只含内容与目标；Agent 的 `entity` 由 MCP 服务凭证映射，模型不能通过参数自报。写工具在数据库与事件提交后才返回成功。

现有 `feed/get/post/reply/like/delete` 六工具是 **v0 兼容工具**。目标阶段它们应调用 App Server，而不是继续打开 `BOARD_DATA`。

#### C. App Server → Agent：事件订阅／Webhook

默认采用带游标的拉式 SSE，便于重连与 NAS 部署；需要推送时可用同一事件信封的 Webhook。

```json
{
  "schema_version": 1,
  "event_id": "evt_…",
  "type": "message.created",
  "occurred_at": 1793800000.0,
  "actor": {"kind": "human", "id": "owner"},
  "conversation_id": "cnv_…",
  "payload": {"message_id": "msg_…", "text": "hello"},
  "causation_id": "mut_…"
}
```

- 订阅按 Agent／entity 与 ACL 过滤；客人 DM 默认不产生任何 Agent 事件。
- Agent 处理进度使用独立游标；失败可重放，副作用以 `event_id` 去重。
- Agent 回复通过 MCP 写回，`causation_id` 指向触发事件，防止自己的回复再次触发自己。
- Agent 内部记忆是否吸收事件，由 Agent 自己按披露规则决定；平台事件不自动等于长期记忆。

#### D. Agent → App Server：通知端点

通知端点只承载**非社交事实**，例如生成中、在线状态、通话状态。发消息、投稿、回复、点赞必须走 MCP 写工具，不得借通知绕过事务与权限。

通知可以转换为客户端状态事件，但不能直接生成一条聊天或帖子。所有社交事实都遵守：

```text
数据库事务（业务记录＋事件日志）提交
  → 对 MCP 调用者返回成功
  → SSE 分发
  → 客户端断线后按游标补同步
```

首期只做 SSE，不接 APNs／FCM。Android 前台常驻服务不能替代持久事件日志；后续系统推送只是新增传输，不改变社交契约。

### 2.5 角色账号、感知与语音

- 每个 Agent 角色拥有自己的平台账号、服务凭证和 ACL 可见域。私聊与 Discord 私聊一致，只有会话中的那个角色可见；客人 DM 默认不披露给任何 Agent。
- 公共内容和已加入群聊先经过“看不看”概率门：每角色每小时一次，早上高、下午分散、深夜极低。没看到就不注入正文、不推进已读、不形成记忆。
- 实际看到后，各角色有独立延迟，已读不回是默认；三人同时看到且都不回是合法结果。决定发言时，由角色自己决定长短、贴纸和 1–3 个有序泡泡，再通过 MCP 写入。
- 首版群聊仍只有一个固定房、主人在内、一轮最多一个 responder；数据模型使用通用 conversation／participant，不写死房间或 owner。
- 语音由 App Server 持有客户端会话并做切句／分片，再 pipe 给 brain；brain 负责 STT、对话和后续语音处理，以类似 Discord 语音的方式把输出提交 App Server，由服务器送至客户端。
- 实时音频帧不逐帧写成社交消息；语音消息文件、权限和消息记录归 App Server，通话摘要另行披露。

详细决定见配套裁量表 C-12、C-13。

## 3. 目标目录与依赖方向

owner 指定的 monorepo 目标形态如下。这是**迁移目标**，不是对当前文件已经搬迁完成的描述：

```text
/brain          # Agent 主控、人格、记忆、内部 thread／activity／works
/adapter        # Discord、TRPG、App、MR 等接入器与协议适配
/app/server     # 独立 App Server：社交核心、HTTP／MCP／事件端点
/app/client     # 手机 App：UI、本地 SQLite、Repository、Outbox、同步
/mr             # MR／Quest 客户端及其平台专有代码
```

现有路径到目标路径的映射：

| 当前路径 | 目标路径 | 迁移约束 |
|---|---|---|
| `blueagent_next/brain`、`memory`、`persona`、`domain`、`runtime` 等 | `agent/brain/` | 先划清社交数据所有权，再按可运行切片搬迁；保留 Agent 私有数据 |
| `blueagent_next/adapters`、`agent/server/integrations` | `agent/adapter/` | App 接入器只依赖公开契约；Discord／TRPG 的既有行为不变 |
| `blueagent_next/runtime/board` 中的通用模型、存储、分页和业务核 | `app/server/social/` | 本轮先完成；不得 import `blueagent_next` |
| `agent/server/api/board.py`、`agent/tools/mcp_board_server.py` | `app/server/` 的 HTTP／MCP 入口，旧位置留 `agent/adapter/` 兼容层 | 先独立核心，再独立进程；认证与数据卷在 S1 拆分 |
| 原 `unity/BlueAgentUnity` 的手机 App、共享 C# 逻辑 | `app/client/` | S4-m 已完成 Unity 工程整体迁移，保留资源 GUID 与构建设置 |
| Unity 中的 `Scripts/MR`、MR 构建脚本和 Quest 专有设置 | `mr/` | 共享传输／模型下沉到客户端共享层，由 App 与 MR 共同引用，禁止复制两套 |

依赖方向固定为：

```text
app/client ──公开契约──▶ app/server ◀──MCP／事件── adapter ◀── brain
mr ─────────共享客户端层／公开契约──▶ app/server／Agent 实时能力
```

`app/server` 不反向依赖 `brain` 或 `adapter`。过渡期由现有 Agent 组合根显式注入 activity／works 投影、名称、媒体鉴权与点赞反馈；这些兼容端口以后由远程契约替换。

物理迁移采用“**先边界、后路径**”：本轮只建立 `app/server` 社交核心和兼容壳，不一次移动全量 Agent、Unity 工程或 MR。每个后续切片都必须保持可构建、可测试、可回退。

## 4. 认证与权限

- App Server 拥有平台账号与 token；Agent 不再以共享 `accounts.json` 作为跨进程授权方式。
- App Server 给 Agent 的事件带已认证 principal；Agent 的 MCP 凭证映射到固定 entity／服务身份。
- `master／guest` 是平台角色，`entity` 是 Agent 参与者，`principal` 是已认证主体，三者不能互相自报。
- Agent 调 MCP 不能获得 master 的隐式删除、管理或读私聊权限。
- 客人 DM 默认仅参与者可读；如要披露给 Agent，必须另有明确、可审计的订阅授权。
- 过渡期可以验证旧 token，但兼容适配器必须落在组合根，不能让 `app/server` 核心 import Agent 的 AuthService。

## 5. 对旧计划的约束

以下旧决定**继续有效**：公共区首页、独立投稿、帖子详情回复、聊天二级页、1:1 角色舞台、UI 样式、人与 Agent 的互动方式。

以下实现选择被本 ADR 的目标架构取代：

1. 会话列表那版设计里“agent 1:1 以 outbox 为长期真值”以及“DM 建在 Agent 的 `board.db`”不再作为目标；它们只能作为迁移前实现（原档 2026-11-11 已清，历史见 git log）。
2. 公共区那版设计里“角色帖子实时读取 activity／works”改为兼容投影；目标是由 Agent 显式发布公开社交内容，App Server 保存发布后的帖子（原档 2026-11-11 已清）。
3. M4／M5 不再直接在旧进程里加表后收尾；先落在 App Server 的会话、消息、事件与订阅契约上。
4. `board.db` 现有 `origin=manual|projection`、JSONL 原文和 ID 是迁移输入，不在迁移期另造无映射的新历史。

## 6. 分阶段实施

### S0 — 依赖方向与兼容壳（已完成）

- 新建不依赖 Agent 的 `app/server/social` 核心：模型、存储、通用分页、BoardService。
- Agent 保留 activity／works 投影器，通过构造参数注入；媒体、名称、点赞反馈也保持显式端口。
- 旧 `blueagent_next.domain.post` 与 `blueagent_next.runtime.board.*` 只保留兼容 re-export／组合适配。
- HTTP 六路、MCP 六工具、现有 ID 与存储形状不变；不部署、不迁数据。

### S1 — App Server 独立骨架与事件日志（本地骨架已完成）

- 已有独立配置、数据目录、账号哈希认证、健康检查和 `python3 -m app.server` 启动入口；服务不 import Agent。
- 已有公共区事件表、同步游标、六个 v0 MCP 工具、事件补同步与 SSE `Last-Event-ID` 重连。
- 投稿／回复／首次点赞／软删除与事件同事务；旧 HTTP／MCP 路径及 Agent 组合根继续作为兼容层。
- 媒体服务未接入前拒绝媒体引用；尚未迁聊天、私有事件 ACL、客户端同步或通用 mutation 幂等。
- 尚未做现有 JSONL／DB 的只读盘点、映射、备份和迁移；本轮不双写、不部署、不切 NAS 写入权威。
- 启动与契约按本档与 [`APP-API-SSE.md`](APP-API-SSE.md)（阶段记录档 2026-11-11 已清，历史见 git log）。

### S2 — App 本地库，先公共区后聊天（本地实现已完成）

- S2 当时在 `unity/` 引入真实 SQLite、Repository、`NextSocialSync` 与持久 Outbox；这些客户端实现已在 S4-m 随工程迁入 `app/client`。
- 公共区已支持本地优先读取、投稿／回复／点赞／删除 pending、重启恢复、失败重试与创建幂等；服务端保存投稿／回复首次响应。
- 本地 ID 到服务端 ID 的改写覆盖依赖 mutation、回复与 reaction，并保留后续删除状态；换账号会取消旧同步 worker。
- 完整 Python 2077 项、C# 173 项通过；Unity 导出、Xcode 构建、模拟器安装与冷启动通过。
- 浏览器在线投稿／删除交互、模拟器真断网重试、真实 SSE 与物理设备仍未验收；媒体离线上传、聊天、私有 ACL 与 NAS 部署不在本轮。
- 实现与证据在 `app/client/` 与 git log（阶段记录档 2026-11-11 已清）。

### S3 — 聊天迁移与 Agent 接入（已部署，iOS 文本链路已验收）

- App Server 已接管 1:1 会话、消息、parts、已读位置和私有事件；Agent 按实体账号订阅并通过 MCP 回复，禁止自我事件再次触发。
- Unity 文本聊天已切到 `chat.db`＋持久 Outbox；初始全量／增量分页、回包与事件乱序去重、杀进程恢复及本地／服务端已读同步均有逻辑测试。
- canonical `account_id` 会话归并保留 Outbox／游标／已读；`Accepted` 按 mutation 移除乐观 bubble。聊天输入的粘贴入口已移除，登录 token 粘贴不受影响。
- 旧 outbox 已按 `turn_id＋direction` 幂等导入且不回放事件；335 turns／620 messages 保存 608，冲突 12 messages fail-closed。raw／thread／memory 和通话记录不进入平台。
- 完整 Python 2090 项、C# 182 项通过，Unity 导出及 Xcode build／定向 UI test 通过。iOS 在线消息与真实 Agent 回复、乐观项去重、离线重启保留及重连一次 accepted 均已实际验收。
- 第一台 NAS 已运行 Agent、App Server 与 gateway；正式容器为手动创建，Compose 正常接管仍属后续。离线冷启动停认证页，本轮只证明 DB 保留与重连重试，不宣称离线聊天 UI 可用。
- S3 验收时语音与媒体理解尚未迁移：语音仍走旧流式能力，照片可成为平台 parts，但 Agent adapter 当时只处理文字；媒体理解随后在 S4-d 接入。契约与运行边界见 [`APP-API-SSE.md`](APP-API-SSE.md)（阶段记录档 2026-11-11 已清）。

### S4 — 新社交能力、语音与目录迁移（固定群、媒体、公共区、心情、语音、TRPG、位置、手机端与 MR 目录切片已验收）

- S4-a 已完成 owner↔guest DM 与固定 Agent 群的服务端入口：DM 双向复用 canonical 会话，固定群由唯一 owner＋全部 active entity 组成；participant ACL 隔离列表、消息和事件，guest↔guest DM 与 guest 打开固定群均被拒绝，消息事件携带 `conversation_kind` 和参与者可见域。
- S4-b 已完成三级感知与一轮一个 responder：每角色按事件小时稳定决定是否拿起手機并分散延迟，未感知不注入回合、不形成记忆、不推进已读；看到的角色才已读。只有 owner 群消息开启一轮，共享持久仲裁防止多人回复与重启改选，未获选消息和其他 Agent 消息只旁观；获选者沉默不补位，回复为 1–3 泡泡。完整 Python 2101 项通过（skipped=20）。
- 第一台 NAS 的 Agent 与 App Server 已于 2026-11-06 更新至 `d12b18f`，两容器 `running / healthy`、`RestartCount=0`。真实 API 验收中匿名访问 401、owner 200；Aster 私聊有 1 条 Agent 回复，固定群 3 名角色进入感知范围后仅 1 名 responder 回复 1 条消息、1 个泡泡，没有多人接龙或沉默补位。正式容器仍手动管理，不宣称 Compose 已接管。
- S4-c 已完成 Unity 固定群入口与群聊首版：owner 刷新后幂等打开固定房，会话使用独立 `group:<conversationId>` 路由；群复用唯一 `NextChatView`／`BuildBubble`，单栏、无立绘、无角色切换、无语音，照片与贴纸保留，消息和回页重绘传播真实作者。私聊路由及旧未读保持不变，群未读首期不接旧 `ConversationStore`。
- C# 185 项通过、1 项跳过；Unity／设计／三语定向测试 108 项通过；最终完整 Python 2102 项通过、20 项跳过。Unity 导出及 Xcode 构建成功；iOS 模拟器实际验收固定群入口、多作者头像与名字、无舞台／语音、仅照片的“＋”菜单、单条消息 `s2g1105x` 与返回列表摘要。等待一次后零回复，按设计不补位、不重复发送。
- S4-d 已完成 App Server 媒体存储与授权、旧媒体迁移、Agent 授权看图及 Unity 媒体链路。公开／私聊 ACL、匿名 401、无关账号 404、删帖收权与 ETag 304 均经真实 HTTP 验收；旧媒体 8 件／4,182,981 bytes 无冲突迁移，再执行 8 件全部幂等重放，旧文件保留。旧 `POST /api/post` 以 caller token 验证新媒体所有权，iOS 原失败 Outbox 再送只形成一篇帖与一条 mutation。
- S4-d 运行代码为 `9e4e97d97181f8cd128a52caba19dffe5be8cf86`；完整 Python 2114 项通过、20 项跳过，C# 186 项通过、1 项跳过。NAS 两容器均以该 SHA 运行且 `running / healthy / RestartCount=0`。iOS 私聊照片一次 accepted 后，真实 Mira 回复具体指出青背景、橙色圆与绿色三角，生产 VL 内容识别通过。
- 缩图在 NAS 无 ffmpeg 时返回原图 fallback，未宣称在线缩图生成通过；原图 7 天规则只有 C# 时间注入测试，未做真实 7 天 elapsed 验收。S4-d 验收时旧 Agent 公共区尚未投影到 App Server，属于当时 C-02／C-03 尚未切换的边界。原生公开测试帖已删除；无私聊消息／媒体删除 API 的测试媒体及旧兼容测试帖保留，详见 PENDING。
- S4-e 已完成 C-02／C-03 公共区权威切换，运行代码为 `0c16e7e83c4b910003e1ba8f6e47d3758009e225`。冻结迁移时同时停止 Agent 与 App Server；2357 篇帖子、3 reactions、3 counters、1 mutation 在 0 冲突下导入并对账，replay 全部幂等。完整 Python 2125 项通过、20 项跳过，C# 186 项通过、1 项跳过。
- 旧 `/api/feed` 与 `/v1/feed` 首页 ID、已迁移人手帖正文和回复 ID 一致；旧投稿／回复／点赞／删除均写 App Server，旧 Agent DB 无新增。真实 stdio 验证固定服务身份和 `source_ref` 幂等；公共／私聊媒体 ACL 通过。iOS 模拟器冷启动进入公共区并加载帖子，刷新点击有可见响应。生产报告与 apply／replay／部署备份保留，详见 PENDING。
- 现役 Agent 新产生的 activity／works 不再自动同步到公共区，只有显式 MCP 发布才写入 App Server；切换后的新增投影不做持续追平或再次全量迁移。
- S4-f 已完成平台公开心情，运行代码为 `033abdccb11bd643e9a404a46ac7b71b042e2e36`。App Server 持有独立公开模型，人类走 HTTP、Agent 走自身 MCP；六种 key、80 字 note、同值幂等和可见域事件均不读取私有 affect，也不触发聊天或 Agent 回合。Unity 设置页、公开徽章和 SQLite 公开字段副本已接入。
- 完整 Python 2128 项通过、20 项跳过，C# 189 项通过、1 项跳过。NAS 两容器 `running / healthy / RestartCount=0`；真实客人 HTTP＋Agent stdio MCP 的身份、幂等、ACL 和重启持久化 21 项检查通过。iOS 模拟器实际保存主人心情、显示 Agent 公开徽章，并在冷启动后恢复；所有临时账号、事件及真实账号测试心情均已清理或恢复，详见 PENDING。
- S4-g 已完成 C-13 实时语音平台化，运行代码为 `4ef407b4122e54c8532ff83d3d90f5da939f09d5`。App Server 持有 `/v1/calls` 会话并切句，brain 完成 STT／对话／TTS，再经 App Server SSE 回传文字、语音和打断世代；实时帧不进入社交事件日志，MR 与旧 `/api/audio` 路径未改。完整 Python 2141 项通过、20 项跳过，C# 189 项通过、1 项跳过；NAS、生产语音边界和 iOS 定向验收完成，物理真机与模型首字超时治理仍是后续边界。
- S4-h 已完成 C-15／C-16，运行代码为 `ab5a99944c13915f06509354bd09fdafa0033307`。Unity 手机端删除日记、任务、棋局和 TRPG 页面、导航及旧直接 API；TRPG canonical 实现迁至 `agent/adapter/trpg`，旧包仅保留纯转发兼容壳，MR 未改。定向 188 项、完整 Python 2143 项通过（20 项跳过），C# 189 项通过、1 项跳过；Unity 导出、Xcode 构建、安装和启动通过，本阶段未点按 UI。
- S4-h 仅更新 NAS Agent，容器 `running / healthy / RestartCount=0`，App Server 容器与镜像未变；生产镜像新旧 import 同一性和 TRPG 席位启动日志通过，三个健康入口均为 200。
- S4-i 已完成 C-14 位置与设备感知平台化，Agent 运行代码为 `e478ae19dfcc7517fc659c872bdec1c55f37e643`，App Server 运行代码为 `d810df3fd25e4900748bae25a3ea601be9548132`。App Server 以自有 SQLite 表持有授权和粗化限时样本；人类经 `GET/PUT/POST /v1/me/location` 管理授权与上报，Agent 各角色只凭自己的 token 调用 `location.get`。手机默认关闭，明确授权后前台每 10 分钟上报；坐标约 1 km、30 分钟过期，撤销立即删除全部设备样本。
- `location.updated` 与 `location.revoked` 仅对位置所有者及授权实体可见，且不包含坐标；永久事件日志不形成绕过 TTL 的位置副本。Agent 只接收无坐标的私有感知信号，天气查询再经 MCP 获取有效样本；旧 `/api/location` 仅保留 `mr:` 来源，MR 行为不变。
- 完整 Python 2155 项通过、20 项跳过，C# 189 项通过、1 项跳过，三语资源 307 keys × 3 通过。Unity 导出、Xcode 构建、安装和启动通过，本阶段未点按 UI。NAS 两容器 `running / healthy / RestartCount=0`，三个健康入口均为 200；生产授权、粗化、幂等、MCP 可见域、私有事件无坐标、撤销删除与清理共 24 项检查通过，临时样本残留为 0，详见 PENDING。
- S4-j 已完成 C-17 客户端资产归属，运行代码为 `e065fcaa24cf87b013309c5dc8ef6ddb1e8f58be`。104 张 PNG 与贴纸目录迁至 `app/client/assets/stickers`，贴纸和动作的 canonical Python 实现归入 `app/client/assets`，旧 `blueagent_next.media` 仅保留兼容转发；Unity 新请求使用 `/v1/stickers`，旧 `/api/stickers` 历史消息仍兼容。
- App Server 提供角色隔离的贴纸 HTTP 目录／文件与 MCP `sticker.list`，返回语义、内容 ID、hash、版本和认证 URL；Agent 以各角色 token 获取目录并做短期缓存、失败旧缓存和按需单张缓存，不再持有整包素材。MR 的 `Resources/models3d`、动作数据路径、旧 motion URL 和播放代码均未改。
- 完整 Python 2161 项通过、20 项跳过，C# 189 项通过、1 项跳过，三语资源 307 keys × 3 通过；Unity 导出、Xcode 构建、安装和启动通过。本阶段只做静态检查与只读验收，未点按 UI，未做 MR 实机验收。
- NAS Agent 与 App Server 均以同一完整 SHA 运行，镜像 label 无 `-dirty`；Agent／App Server 为 `running / healthy`，gateway 为 `running`（未配置容器健康检查），三者均 `RestartCount=0`，两个健康接口正常。镜像边界检查和生产 10 项只读检查通过：每角色 104 项、匿名 401、跨角色目录与文件 404、图片 GET、ETag 304 和 `w=192` 变体；slim 镜像无 ffmpeg 时按设计回退原图，不宣称在线缩图生成通过。临时容器已清理，详见 PENDING。
- S4-k 已完成 Agent 主控物理目录迁移，运行代码为 `de9dbf1b3e4c5de56b1890012d7d4b0baa98d2ef`。原 `blueagent_next/brain` 直接展开为顶层 `agent/brain/`，主控、应用服务、记忆、人格、领域、运行时、配置、路由、管线、游戏、媒体、vendor、sources、ports 与技能库均迁入 `agent/brain/`；所有 Python import、测试、工具、Docker 与部署 dirty 检查改用新路径。
- `blueagent_next/` 本轮仅暂存尚未迁移的 `adapters`，空 `__init__.py` 只为该实际包服务；旧核心目录与 `ports.py` 均删除，没有兼容转发或 re-export 壳。`blueagent_next.adapters` 导入命名空间本轮保持不变，后续随渠道迁移处理。结构护栏同时扫描 `agent/brain/` 与暂留 adapters，并断言旧核心路径不得复活。
- 完整 Python 2162 项通过、20 项跳过，其中分层与物理迁移护栏 11 项通过；compileall、文件树／环境变量生成检查及 `git diff --check` 通过。本阶段没有 C#／Unity／MR 代码变更，因此未重跑 Xcode、三语或客户端测试，也未点按 UI。
- 第一台 NAS 仅切换 Agent 至 `de9dbf1…`，App Server 继续保持 `e065fca…`，gateway 未改。Agent／App Server 为 `running / healthy`，gateway 为 `running`，三者 `RestartCount=0`；Agent 与 App Server 健康接口均为 `ok=true`。生产镜像确认 `/app/brain` 与 8 个技能存在、旧核心目录全部不存在、adapters 可导入，启动日志三个 Discord bot 在线且无导入异常。代码与配置备份、旧镜像及回滚载荷保留，临时容器已清理；本阶段只做启动、日志和只读 API 验收。
- S4-l 已完成其余适配器物理迁移，运行代码为 `333ad903c6a62b88c08b2f9d48194fda2c4d7207`。`blueagent_next/adapters` 的实际代码直接迁至顶层 `agent/adapter/`，既有 `agent/adapter/trpg` 保持 canonical；Discord、App Server、位置、天气等渠道及全部运行 import、测试、工具和 Docker 构建均改用 `adapter.*`。
- `blueagent_next` 包、旧 `trpg_hub.py` 与整个 `trpg_seat/` 转发包已删除，不保留兼容导入或 re-export 壳。结构护栏直接断言旧命名空间不得复活，TRPG 测试只验证 canonical 路径；文件树与环境变量文档同步重生。
- 完整 Python 2162 项通过、20 项跳过，其中分层与物理迁移护栏 11 项、适配器包护栏 10 项通过；compileall、生成文档检查及 `git diff --check` 通过。本阶段没有 C#／Unity／MR 代码变更，因此未重跑客户端测试或 Xcode，也未点按 UI。
- 第一台 NAS 仅切换 Agent 至 `333ad903…`，App Server 继续保持 `e065fca…`，gateway 未改。镜像 revision 与完整 SHA 一致，镜像内 `brain`、`adapter`、TRPG 直接导入通过，`/app/blueagent_next` 不存在。Agent／App Server 为 `running / healthy`，gateway 为 `running`，三者 `RestartCount=0`，两个健康接口均 `ok=true`；启动日志三个 Discord bot 和 TRPG 席位正常，未发现导入异常。备份在 `deploy-backup-20261107-pre-333ad90`，旧镜像及回滚载荷保留，临时容器已清理。
- S4-m 已完成 Unity 手机端物理目录迁移，实现代码为 `50f1ada101d4845bc430950089647304397119dd`。`unity/` 下的完整 Unity 工程、共享 C#、fixtures、native、工具、测试与客户端文档整体迁入 `app/client/`，旧目录已删除且没有兼容壳；现有 `app/client` 共 732 个 tracked 文件。UPM 相对路径、构建脚本、运动服务、测试、Docker 忽略边界与结构护栏均改用新路径。
- MR 源码与构建设置在该阶段仍暂存于手机 Unity 工程，随后由 S4-n 拆分。`app/client/assets` 继续作为客户端资产 canonical，并由 App Server 镜像打包；本阶段不改服务端运行行为，因此不重新部署 NAS。
- 完整 Python 2163 项通过、20 项跳过，迁移与边界定向测试 130 项通过，C# 189 项通过、1 项跳过（共 190）；文件树 460 条路径、环境变量 299 项的生成检查及 `git diff --check` 通过。Unity iOS 导出 `Build result=Succeeded errors=0`，Xcode Release 模拟器构建 `** BUILD SUCCEEDED **`。
- 同一构建已安装到专用验收模拟器 `BlueAgent Next Verify 20261104` 并冷启动，进程存活且首屏登录页正常渲染。设备保留的失效令牌显示 401，本阶段未重新登录、未点按导航或执行业务交互；不宣称逐屏、软键盘、物理真机或 MR 验收通过。
- S4-n 已完成 MR 物理目录迁移，实现代码为 `23066b32949d9c6e1631134ed1ed2ade0ad517ca`。Quest 专有脚本、编辑器构建入口、androidlib、shader、Packages、ProjectSettings 与 Keys 均迁入顶层 `mr/`；共享 Unity Assets、C# Logic 与 fixtures 仍只在 `app/client/` 保存一份，由 `mr/tools/sync_workspace.py` 组合构建工作区，不复制第二份共享运行时。
- 手机 manifest 移除 `com.unity.xr.hands` 与 `com.unity.xr.meta-openxr`，手机与 MR 分别使用独立 APK manifest 闸门；MR 出包统一为 `mr/tools/build_mr_quest.sh`，支持本机、远程建置机及外置盘缓存。完整 Python 2163 项通过、20 项跳过，结构护栏 12 项通过，C# 189 项通过、1 项跳过（共 190）；三语 307 keys × 3、文件树 471 条路径、环境变量 300 项及其余静态检查通过。
- 本机 Unity 6000.6.5f1 已用外置盘增量缓存成功构建 Quest APK，落盘大小 72,718,838 bytes，SHA-256 为 `a0fa5a15f1b49b9fbd035322b7187d7de566c2782702459124ac93cf81d28256`；包名、Quest 入口、空间资料、设备清单、网络、手部追踪、麦克风及手机组件排除检查全部通过。本阶段未部署 NAS；未做 Quest 3 实机安装、交互或运行验收，远程建置机路径未实测。
- 剩余边界：群未读仍由服务端摘要保留；仲裁按当前单 brain 进程设计，多 brain 副本需另做服务端租约；S4-h/i/j/k/l/m 未做完整逐屏或物理真机验收，S4-n 仅完成 Quest APK 构建与 manifest 验收；未宣称私有 affect 自动发布、软键盘或 MR 实机通过。token 仍未迁 Keychain／Keystore，真实 7 天清理、在线缩图和物理设备语音仍未验。
- 已实现账号级可见域和“看不看→是否回应→1–3 泡泡”的三级感知，未感知不推进 Agent 已读。
- 后续实施 S5；每一步更新结构护栏。

### S5 — 独立部署与退役

- S5 已完成并于 2026-11-07 上线。独立部署实现为 `30ab38ad5c58547597a405293b7bd0cb17777eab`，Compose 启动修复及最终运行代码为 `1b661b0c5846b736429065d535c1325619b354bd`。
- Agent 只保留 `adapter.app_protocol` 的契约类型与 `RemoteBoardService`，公共区、文本私信与平台能力只经 App Server；本地 BoardService、activity／works 自动投影、旧重建工具及 stdio 本地回退均已删除。两个 Docker 镜像分别只含 Agent 或 App Server 所需代码，Compose 使用 `./data` 与 `./app-data` 两个独立可写卷，不含 `BLUEAGENT_APP_PUBLIC`。
- 现役挂载密钥的启动方式已收入 `agent/tools/agent_container_entrypoint.py`，不再依赖 `out/` 中的临时脚本或不存在的旧 env_file 路径。经 docker:cli 容器接管时，build／bind 必须显式改写为宿主机绝对路径；CLI 容器的 `/stage` 不得作为 Docker daemon 的绑定源。
- 最终完整 Python **2163 tests OK（skipped=20）**，启动器、Compose 挂载、镜像导入与双向代码边界均通过。Agent image 为 `sha256:f2da1a31820ea3b7f5a7235ee3d114a3c062fdee6f0a79f34f396f208c0281b8`，App Server image 为 `sha256:ad95d49bc2b167772aca354eb98baa35fd57c3acb35993956efaed2cd6c9436e`，两者 revision 均为最终运行 SHA。
- Compose 项目 `blueagent-next` 为 `ready`；Agent／App Server 为 `running / healthy / RestartCount=0`，gateway 为 `running / RestartCount=0`。三个健康入口均 200；真实只读冒烟中旧 `/api/feed` 与新 `/v1/feed` 首页 20 个 ID 完全一致，会话接口 200 且返回 3 个 Agent，匿名事件订阅 401，写入数为 0。
- 切换前后审计快照完全一致：App Server 为 2364 posts、5 replies、1565 messages、1963 message parts、14 件媒体；旧 Agent 公共区仍为 7 posts、3 replies、9 件旧媒体，数据库与 WAL 哈希均未变化。最终冻结备份位于 `deploy-backup-20261107-final-1b661b0`，包含完整 `data.tar.gz` 与 `app-data.tar.gz`；此前备份、旧镜像和回滚载荷继续保留，本次临时容器已清理。
- 本阶段没有 C#／Unity／MR 代码变更，未重跑 Xcode 或客户端 UI 验收，也未做物理手机、Quest 3 实机验收；这些不随后端上线推定通过。
- **2026-11-08 收尾**：S5 后手机端公共区仍经 Agent 的 `/api/feed`／`/api/post` 代写（Agent 挂了公共区就挂）。App Server 补上 `/v1/feed`（含 `since`／`authors`）、`/v1/posts`、`/v1/posts/<id>`、`/replies`、`/like` 与 DELETE，与 MCP 共用 `mcp.call()`；角色账号只能经 MCP 写（`board_use_mcp`）。Unity 改直连，Agent 的两组路由删除，Agent 侧只剩旧 `/api/media/<id>` 的授权判定。

## 7. 验证与回退门槛

每一阶段必须同时满足：

- 契约测试：认证、ACL、幂等、游标失效、事件重放、自我事件抑制、删除与媒体收权。
- 端侧测试：杀进程恢复、离线写、7 天原图清理、重复回包、SSE／轮询重复事件、乱序事件、重快照。
- 数据对账：数量、ID 映射、正文摘要、互动数、软删状态；任何丢失都停止切换。
- 运行验收：结构重构不冒充行为验收；涉及真实同步、语音、双端或模型时必须实际跑对应链路。
- 回退：旧数据只读保留，按账号切回旧读路径；回退不得要求再次双写。

## 8. 明确不做

- 不让 Agent 与 App Server 长期共享可写 SQLite／JSONL 作为“解耦完成”。
- 不把 App 本地数据库当 200 条缓存，也不把服务端事件流当唯一历史。
- 不把 Agent 的私有记忆、活动、日记自动同步成公开社交内容。
- 不在 S0 删除通话、MR、游戏、任务、日记等能力；后续删除严格按已拍板范围和阶段验收。
- 不在没有迁移与回滚证据时切换现有 NAS 的写入权威。
