"""組合根 `App` 的型別（架構審查 E3，2026-11-17 從 `bootstrap.py` 拆出）。

`bootstrap.build()` 組好之後交給 `AppState`；伺服器的整合層（Discord／TRPG／告警／身分）啟動時會再掛幾個欄位。
**每一個會被設定的欄位都要在這裡宣告**——以前有五個是執行期臨時 `setattr` 上去的（`identity`、`trpg`、
`trpg_seats`、`alerts`、`discord_by_entity`），讀的人只能 `getattr(app, "x", None)` 猜它在不在。
`tests/test_app_root_fields.py` 掃全庫的 `app.<名字> = …`，沒宣告就紅。

型別多半還是 `object`（漸進收緊；E3 的其餘部分）。
2026-11-17 第二片（`NEXT-STEPS-20261117.md` WP-C）：身分／記憶邊界收緊——`entities`／`principals`
用現有的具體類別、`memory` 用 `ProbeMemoryPort`（三種後端共同的契約，不綁某一個後端）、`accounts`／`identity`
標成具體類別或 None。只動標註：型別都在 `TYPE_CHECKING` 底下 import，執行期與裝配順序不變；
`tests/test_app_root_fields.py` 鎖「標註指得到真的類別、組好的 App 實際就是這些型別」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:                      # 只給型別檢查看：執行期不 import，免得組合根拉一串循環依賴
    from agent.brain.compose.identity import IdentityMap
    from agent.brain.domain.entity import EntityRegistry
    from agent.brain.domain.principal import PrincipalRegistry
    from agent.brain.ports import ProbeMemoryPort
    from agent.brain.runtime.identity.accounts import AccountStore
    from agent.brain.router.policy import Router
    from agent.brain.runtime.activity_store import ActivityStore
    from agent.brain.runtime.attention import AttentionGate
    from agent.brain.runtime.entity_store import EntityStore
    from agent.brain.runtime.episodes import EpisodeArchive
    from agent.brain.runtime.identity.auth import AuthService
    from agent.brain.runtime.identity.guest_store import GuestStore
    from agent.brain.runtime.ingress import IngressRuntime
    from agent.brain.runtime.pending_store import PendingInterventionStore
    from agent.brain.runtime.scheduler import Scheduler
    from agent.brain.runtime.stance import StanceService
    from agent.brain.runtime.stance_store import StanceStore
    from agent.brain.runtime.threadstore import ThreadStore
    from agent.brain.runtime.tools import ToolRegistry
    from agent.brain.runtime.voice import VoiceService


@dataclass
class App:
    entities: EntityRegistry
    principals: PrincipalRegistry
    #: 記憶後端：`memory/in_memory`、`adapters/memory_sqlite`、`adapters/memory_hindsight` 其一（共同契約 `ProbeMemoryPort`）
    memory: ProbeMemoryPort
    brain: object
    threads: ThreadStore
    entity_store: EntityStore
    scheduler: Scheduler
    pipelines: object
    router: Router
    runtime: IngressRuntime
    archive: EpisodeArchive
    social: object
    activity_store: ActivityStore
    #: 任務台帳 `{entity: TaskLedger}`（owner 端看板／日曆、她的 `tasks` MCP 工具共用同一份）
    ledgers: dict
    #: 每個角色一條 LifeLoop（各自一條執行緒、各自的活動與檢查點）
    lives: dict
    step_fn: Callable
    voice: VoiceService
    tools: ToolRegistry
    attention: AttentionGate
    pending_store: PendingInterventionStore
    guest_store: GuestStore
    stance_store: StanceStore
    stances: StanceService
    auth: AuthService
    #: 端末上報的座標（天氣用；按端末存，見 runtime/locations.py）
    locations: object
    #: 背景提煉器（`BLUEAGENT_DISTILL=1` 才有；見 runtime/distill.py）
    distiller: object = None
    #: 每日資料快照（`runtime/ops/backup.py`）
    backup: object = None
    #: 語意層：embedder（`adapters/embedder.py`）＋向量倉（`runtime/memory/vector_store.py`）
    embedder: object = None
    #: 檢索重排（`runtime/memory/rerank.py`）
    reranker: object = None
    vectors: object = None
    #: 每個 user 一頁 `user.md`（檔案層）
    userfile: object = None
    #: 語音輸入（STT）。None＝沒裝/沒開，`/api/audio` 回 503。
    stt: object = None
    #: 通話裡「還沒講完」的那句先扣住、跟下一句併（`runtime/voice/speech_hold.py`）
    speech_hold: object = None
    #: 贴纸语义目录（App Server 远程；本地开发回退 `app/client/assets/stickers/`）
    stickers: object = None
    #: 動作片段庫（`app/client/assets/motion.py`；`$BLUEAGENT_DATA_DIR/motion/`）
    motion: object = None
    #: 圖片倉（`media/store.py`）：App 上傳、以及**委任出圖**的成品都存這裡（兩端用 URL 拿）
    media: object = None
    #: 主人要她交付、她還沒交的東西（`runtime/inner/pending_delivery.py`，owner 2026-11-08）
    pending_delivery: object = None
    #: 她送出去的檔案倉（`brain/media/files.py`）：`file.send` 寫、Discord transport 反查本機檔案
    files: object = None
    #: 固定段指紋（`fixed_hash`／`fixed_chars`／`entity`／`at`）：`/api/metrics` 用。
    prompt_stats: dict = field(default_factory=dict)
    #: 人格 core（`{entity: core}`）：SPIN 第 ① 段編譯結果，`PersonaStage` 讀它貼 prompt。
    persona_cores: dict = field(default_factory=dict)
    #: 交付出匣（斷線可補）：每輪的文字／音檔留一份
    outbox: object = None
    #: 帳號層（master／guest）：Unity 的 master 端與 guest 端靠它認身分
    accounts: AccountStore | None = None
    #: Discord 源（bot）。None＝沒 token，不連。
    discord: object = None
    #: 每輪的結構化紀錄（`data/logs/turns.jsonl`）＋ `/api/metrics`。
    telemetry: object = None
    #: 推送匯流排（App 的 `GET /api/events` 長連；後續交付／主動訊息走這裡）。
    bus: object = None
    #: 她的行動日誌（private／public 兩種可見度）。
    activity_log: object = None
    #: 主動消息的閘（安靜時段／最小間隔／日上限）。
    proactive: object = None
    #: 情緒狀態（AEM 適配層）。
    affect: object = None
    #: 主動發言的信道選擇（App／Discord DM／群）。
    delivery: object = None
    #: 行動預算（她自己的精力額度）。
    budget: object = None
    #: 遊樂場（自己的＋共用的）。
    playground: object = None
    #: 心跳（她自己維持活性）。
    heartbeat: object = None
    #: 各自的生物鐘（owner 2026-11-08）：`{entity: CircadianClock}`；空＝這版沒有。
    circadian: object = None
    #: 主人打 App 電話時「接不接」（`runtime/inner/call_pickup.py`，owner 2026-11-17）；None＝一律接。
    call_pickup: object = None
    #: `data/` 根（日誌、封存都在這底下）；匯報落檔用
    data_dir: object = None
    #: 活動輪換（活動庫挑選；`activity_api.reload` 重建 loop 時要接回去）
    rotation: object = None
    #: 排下一件事的函式（`LifeLoop` 收尾時呼叫；重建 loop 時接回去）
    next_activity: object = None
    worlds: dict = field(default_factory=dict)  # 世界觀察者與短期事件匯流排
    world_events: object = None
    # ── 啟動後才由整合層掛上的（E3 第一步：以前是執行期臨時 setattr，這裡正式宣告）─────────
    #: 平台帳號 ↔ 腦端 principal 的對照（`compose/identity.py`，DESIGN §3.2）
    identity: IdentityMap | None = None
    #: TRPG 席位登記表與預設席（`server/integrations/trpg.py`）
    trpg_seats: object = None
    trpg: object = None
    #: 告警通道（`server/integrations/alerts.py`）
    alerts: object = None
    #: 每個角色自己的 Discord bot（`server/integrations/discord.py`；`discord` 是預設那一隻）
    discord_by_entity: dict = field(default_factory=dict)

    # ── 相容與便利 ────────────────────────────────────────────────────────────
    @property
    def life(self):
        """預設角色那一條 LifeLoop（舊呼叫端相容）。多角色請走 `lives`。"""
        default = self.entities.default()
        target = default.id if default is not None else "mira"
        return self.lives.get(target)

    def shutdown_lives(self) -> None:
        """把所有角色的 LifeLoop 都停掉（測試／進程收尾用）。"""
        for loop in list(self.lives.values()):
            try:
                loop.shutdown()
            except Exception:                          # noqa: BLE001
                pass


__all__ = ["App"]
