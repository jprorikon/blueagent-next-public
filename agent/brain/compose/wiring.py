"""組合根的分段接線：記憶後端、旁路 LLM、語意檢索、工具面、客人還原。

從 `bootstrap.build()` 拆出來（2026-10-25 結構 review 第 3 階段）。每一支只做一件事、
依賴全部從參數進來；`build()` 只負責照順序呼叫它們。
"""

from __future__ import annotations

import os
import pathlib
import threading
import time
from dataclasses import dataclass

from agent.brain.domain.principal import Principal
from agent.brain.memory.in_memory import InMemoryMemory

# agent/ 根：MCP server 的工作目錄。`data/mcp.json` 裡的 `tools/mcp_*.py` 相對它（2026-11-09 搬進 agent/ 後舊設定照用）
ROOT = pathlib.Path(__file__).resolve().parents[2]


def _log(message: str) -> None:
    print(f"  {message}", flush=True)


# ── 記憶 ──────────────────────────────────────────────────────────────────────
def build_memory(base, *, store, names, social, entity_ids, entity_names=None):
    """記憶後端：預設記憶體 gram；`BLUEAGENT_MEMORY=sqlite|hindsight` 換索引（原文照樣進 facts.jsonl）。

    `entity_names`（角色 id → 顯示名）給「跨角色轉述」用：鏡像進來的那條提起時要標「聽誰說的」。
    """
    backend = str(os.environ.get("BLUEAGENT_MEMORY") or "").strip().lower()
    if backend in {"hindsight", "hs"}:
        # 與現役 blueagent 同一個記憶框架：Hindsight bank（語義召回）＋ 本地 SQLite（原文/ACL）
        from agent.adapter.memory_hindsight import HindsightMemory
        return HindsightMemory(
            base / "memory" / "facts.db", store=store, names=names, social=social,
            host=str(os.environ.get("HINDSIGHT_HOST") or "http://127.0.0.1:8897"),
            prefix=str(os.environ.get("MEM_BANK_PREFIX") or "blueagent"),
            timeout=float(os.environ.get("MEM_TIMEOUT") or 2.0),
            top_k=int(os.environ.get("MEM_TOP") or 8),
            context=str(os.environ.get("MEM_CONTEXT") or ""),
            entity_ids=entity_ids, entity_names=entity_names)
    if backend in {"sqlite", "fts", "index"}:
        from agent.adapter.memory_sqlite import SqliteMemory
        return SqliteMemory(base / "memory" / "facts.db", store=store, names=names,
                            social=social, entity_ids=entity_ids,
                            entity_names=entity_names)
    return InMemoryMemory(names=names, social=social, store=store, entity_ids=entity_ids,
                          entity_names=entity_names)


#: 她對人說的話不算「經歷」：這些已經進 threads 與上下文，再沉成事實等於自我強化
CONVERSATIONAL_KINDS = {"heartbeat", "voice", "trpg-report"}


def make_public_sink(memory):
    """行動日誌的 public 那些同時沉進記憶（她自己的經歷）。

    owner 2026-10-24 修的三件事：
     ① **綁 entity**：以前沒傳 entity ⇒ 存成 entity='' 的「通用事實」，`_fact_pool` 的
        `entity = ? OR entity = ''` 會把它塞進**每個角色**的上下文（實測：nori 照抄了
        mira 那句「役割カードのことちゃんと考えてるよ」）。
     ② **當事件、給 `EVENT_TTL_DAYS` 的壽命**（預設 3 天，owner 2026-11-16 從 1 天調整）：這些是「今天做了什麼」，不是永久人格；以前 valid_to=None
        永不過期 ⇒ 同一件事會一直掛在嘴邊（她 4 次主動提「角色卡」就是這樣滾起來的）。
     ③ **她對人說的話不算「經歷」**（`CONVERSATIONAL_KINDS`）。
    """

    def sink(entity: str, text: str, entry: dict) -> None:
        if str(entry.get("kind") or "") in CONVERSATIONAL_KINDS:
            return
        from agent.brain.runtime.distill import event_valid_to
        happened = float(entry.get("at") or 0) or time.time()
        memory.add(text, layer="self", label="她的经历", self_owned=True,
                   entity=str(entity or ""), audience=None, kind="event",
                   valid_from=happened, valid_to=event_valid_to("event", 0, happened),
                   source="activity_public",
                   # 作品 vs 經歷（owner 2026-11-16）：公開日誌那條也繼承活動的 fiction 標記
                   fiction=int(entry.get("fiction") or 0))

    return sink


# ── 旁路 LLM（狀態引出／內心話／重排）────────────────────────────────────────
@dataclass
class SideCalls:
    """大腦以外的短呼叫通道。遠端沒 key ⇒ 各通道是 None（對應的階段自己跳過）。"""

    elicit: object = None
    sub: object = None
    sub_enabled: bool = True


def _offline_step_call():
    """**測試基座**（owner 2026-11-03）：生活步進用的離線替身，一句固定的日語、不碰網路。

    為什麼要有：demo 建的 app 會拿真的 `http_call` 當 `step_call`，於是每一輪測試都在撥
    `data/llm.json` 指的端點——端點一慢，全量從 2.5 分鐘變成 30 分鐘，而且「等 2 秒 cursor
    要動」那幾條測試跟著飄（2026-11-03 實測一次全量 4,962 次 TimeoutError）。
    只有測試進程會設 `BLUEAGENT_STEP_CALL_OFFLINE=1`，生產不設就照舊走真端點。
    """
    def _call(_prompt: str) -> str:
        return "（オフライン）手を動かして、分かったことを一つ確かめた。"
    return _call


def build_side_calls() -> SideCalls:
    """`data/llm.json` 的角色設定 → 三支短呼叫（引出可以指到另一顆模型）。"""
    from agent.brain.persona.core import http_call
    from agent.brain.config import llm as llm_config
    try:
        llm_config.apply_env_defaults()
        for row in llm_config.summary():
            print(f"  llm[{row['role']}]: {row['model']} (max_tokens {row['max_tokens']},"
                  f" temp {row['temperature']})", flush=True)
    except Exception as exc:                     # noqa: BLE001 - 設定檔壞了不擋啟動
        print(f"  llm: 設定檔讀取失敗（{type(exc).__name__}），沿用 env", flush=True)
    # 引出只要一坨短 JSON ⇒ 給小預算；可以用**另一顆模型**（`BLUEAGENT_STATE_LLM_*`）
    state_env = dict(os.environ)
    state_base = str(os.environ.get("BLUEAGENT_STATE_LLM_BASE_URL") or "").strip()
    if state_base:
        state_env["BLUEAGENT_LLM_BASE_URL"] = state_base
        state_env["BLUEAGENT_LLM_MODEL"] = str(
            os.environ.get("BLUEAGENT_STATE_LLM_MODEL") or "qwen38-27b")
        state_env["BLUEAGENT_LLM_API_KEY"] = str(
            os.environ.get("BLUEAGENT_STATE_LLM_API_KEY") or "local")
    # DeepSeek 會先吐 reasoning（實測同一句 898 reasoning tokens）⇒ 額度給太小就只剩空字。
    state_role = llm_config.settings_for("state")
    elicit = http_call(state_env, max_tokens=int(state_role.get("max_tokens") or 1200),
                       disable_thinking=True)
    sub_enabled = str(os.environ.get("BLUEAGENT_SUBAGENTS") or "1").strip().lower() \
        not in {"0", "false", "no", "off"}
    # owner 2026-10-23：子代理的內心話也走 DeepSeek（額度小，一句 200 tokens 級）
    sub_role = llm_config.settings_for("subagent")
    sub = http_call(dict(os.environ), max_tokens=int(sub_role.get("max_tokens") or 1200),
                    disable_thinking=True)
    if str(os.environ.get("BLUEAGENT_STEP_CALL_OFFLINE") or "").strip().lower() \
            in {"1", "on", "true", "yes"}:
        # 測試基座：不撥端點，回一句固定的日語（生產不設這個變數）
        sub = _offline_step_call()
    return SideCalls(elicit=elicit, sub=sub, sub_enabled=sub_enabled)


# ── 語意檢索 ─────────────────────────────────────────────────────────────────
@dataclass
class Search:
    memory_search: object
    embedder: object = None
    vectors: object = None
    reranker: object = None


def build_search(base, *, memory, raw_log, entity_ids) -> Search:
    """她自己的記憶檢索：本機 embedding ＋ 向量倉（背景暖機、每 2 分鐘增量建索引）＋ 重排。"""
    from agent.adapter.embedder import Embedder
    from agent.brain.persona.core import http_call
    from agent.brain.runtime.memory.memory_search import MemorySearch
    from agent.brain.runtime.memory.rerank import Reranker
    from agent.brain.runtime.memory.vector_store import VectorStore
    embedder = Embedder()
    vectors = VectorStore(base / "memory" / "vectors.db", embedder=embedder)
    if str(os.environ.get("BLUEAGENT_EMBED") or "1").strip().lower() in {"0", "false", "no", "off"}:
        embedder = None
        vectors = None
    memory_search = MemorySearch(memory=memory, raw_log=raw_log, vectors=vectors)
    if vectors is not None:
        _start_index_workers(memory_search, vectors, raw_log, entity_ids)
    reranker = None
    # 檢索重排：用同一支短呼叫（DeepSeek、低 token）；`BLUEAGENT_RERANK=0` 可關
    if str(os.environ.get("BLUEAGENT_RERANK") or "1").strip().lower() not in {"0", "false", "no", "off"}:
        # 判官要比內心話更寬的額度（思考型模型會先花 token；實測 1200 會被截成 unparsable）
        rerank_call = http_call(dict(os.environ), max_tokens=2000, disable_thinking=True)
        reranker = Reranker(rerank_call,
                            timeout_s=float(os.environ.get("BLUEAGENT_RERANK_TIMEOUT_S") or 2.5),
                            cache_ttl_s=float(os.environ.get("BLUEAGENT_RERANK_CACHE_S") or 60))
        memory_search.reranker = reranker
    return Search(memory_search=memory_search, embedder=embedder, vectors=vectors,
                  reranker=reranker)


def _start_index_workers(memory_search, vectors, raw_log, entity_ids) -> None:
    def warm() -> None:
        try:
            result = memory_search.warmup(entity_ids)
            print(f"  embed: 暖機完成 {result.get('counts')}", flush=True)
        except Exception as exc:                       # noqa: BLE001 - 暖機失敗不擋服務
            print(f"  embed: 暖機失敗（{type(exc).__name__}: {exc}）", flush=True)

    def index_loop() -> None:
        """背景增量建索引：每 2 分鐘把最近 40 輪＋新事實補進向量倉。

        查詢路徑只讀快取（不嵌入），所以索引一定要有人持續補，否則新對話永遠搜不到。
        """
        while True:
            try:
                for entity_id in entity_ids:
                    rows = raw_log.recent(entity_id, limit=40)
                    vectors.sync("turn", entity_id,
                                 [(f"{row.get('thread') or ''}#{row.get('turn') or row.get('at')}",
                                   str(row.get("text") or "")) for row in rows])
                    # 兩條流各自增量（分流後只取混池最新 200 條 ⇒ 對話事實永遠沒有向量）
                    for stream, cap in (("dialogue", 600), ("life", 200)):
                        pool = memory_search._fact_pool(entity_id, {str(entity_id)},
                                                        cap=cap, stream=stream)
                        vectors.sync("fact", entity_id,
                                     [(fact.text, fact.text) for fact in pool])
            except Exception as exc:                   # noqa: BLE001 - 索引壞了不能擋對話
                print(f"  embed: 增量索引失敗（{type(exc).__name__}: {exc}）", flush=True)
            time.sleep(float(os.environ.get("BLUEAGENT_EMBED_INDEX_S") or 120))

    threading.Thread(target=warm, name="embed-warmup", daemon=True).start()
    threading.Thread(target=index_loop, name="embed-index", daemon=True).start()


# ── 工具面 ───────────────────────────────────────────────────────────────────
def register_tools(registry, *, base, entities, delegate, playground, locations, activity_log,
                   memory_search, skills, stickers=None, media=None, files=None):
    """回建立好（或傳進來）的圖片倉：委任出圖要存進去，App 也要用它（`state.app.media`）。

    `files`＝她送出去的文件倉（`brain/media/files.py`）；Discord 那側要用同一個實例反查本機檔案。
    """
    if media is None:
        from agent.brain.media.store import MediaStore
        media = MediaStore(base / "media")
    if files is None:
        from agent.brain.media.files import FileStore
        files = FileStore(base / "files")
    """她的工具面。**註冊順序就是模型看到的順序**——跟拆出來之前一模一樣，別隨手調。"""
    from agent.adapter.mcp_tools import build_tools
    from agent.adapter.weather_tool import GetWeatherTool
    from agent.brain.runtime.tools import (DelegateTool, MemoryOpenTool,
                                     MemorySearchTool,
                                     NotesReadTool, PlaygroundTool, SkillViewTool,
                                     StickerSendTool,
                                     VoiceCallTool, VoiceHangupTool, VoiceSpeakTool)
    registry.register(VoiceCallTool()).register(VoiceSpeakTool()).register(VoiceHangupTool())
    # 委任本體：主人說 work 模式時，她真的能叫 Hermes 去這個家的機器上幹活。
    if delegate is not None and delegate.enabled:
        registry.register(DelegateTool(delegate, media=media))
    registry.register(PlaygroundTool(playground))
    # 位置倉＋天氣工具（owner 2026-10-23：沿用現役 BlueAgent 的 get_weather）
    registry.register(GetWeatherTool(locations))
    # 搜尋／正文／整站抓取統一走自架 Firecrawl MCP；JS 頁面走 terminal 進角色自己的 VM。
    # 公共區工具面不另寫第二套：走現役 MCP `board`（`tools/mcp_board_server.py`，8 支工具）。
    # owner 2026-11-07：「自動作從帖讀」⇒ 自主動作用它的 feed/get/reply/like（見 SELF_WORK_TOOLS），
    # 「公共活動記錄」（`activity.peek`）已廢除。回帖／點讚的每日上限在 MCP server 那側生效。
    # 讀自己的筆記正文（複習／自測用；只讀自己的、private 段落不回流）
    registry.register(NotesReadTool(base / "notes"))
    # 她自己的 VM（owner 2026-11-03）：只在 work 模式出現——聊天裡不給她開 shell
    from agent.brain.runtime.tools.shell import TerminalTool
    registry.register(TerminalTool(base / "vm", audit_root=base / "_ops"))
    registry.register(MemorySearchTool(memory_search))
    # 門：search 吐門牌、open 吃門牌——少了這支，`ptr` 只是一串好看的代號
    registry.register(MemoryOpenTool(memory_search))
    registry.register(SkillViewTool(skills))
    # 送檔案（owner 2026-11-08）：只在自己的三個根底下挑，只在 Discord 出現（App 還沒有檔案泡泡）
    from agent.brain.runtime.tools import FileSendTool
    registry.register(FileSendTool(files, base=base))
    # 排前面（理由同 sticker.send：28 支工具時，排在後面的等於不存在）
    registry.prioritize("file.send")
    # 表情包（owner 2026-10-29「雙向」）：只在 App 的聊天畫面給她（`schema_for` 按角色／來源現算）
    if stickers is not None:
        registry.register(StickerSendTool(stickers))
        # ⚠️ **必須排到清單最前面**（2026-10-29 真機實測）：
        # 模型的工具清單有十幾支時，**排在後面的等於沒有**——
        # `sticker.send` 排第 11 位時，實測 **12/12 輪她都不調**（每一輪都正確理解了
        # 主人那張貼圖的意思，就是從不主動回）；排到最前面、或只給 6 個工具時，**每次都調**。
        # ⇒ 順序不是美觀問題，是功能問題。
        registry.prioritize("sticker.send")
    # App 的聊天前後文與帖子由她自己問 App 取（owner 2026-11-08「从 app 工具中去取，包括回复和 post」）：
    # App 是真值，大腦那份對話記錄只是她看過的副本。只在 App 渠道出現、排在前面（理由同上）。
    from agent.adapter.app_read_tools import build_app_read_tools
    names = {entity.id: str(getattr(entity, "name", entity.id)) for entity in entities.all()} \
        if hasattr(entities, "all") else {}
    for tool in build_app_read_tools(names=names):
        registry.register(tool)
        registry.prioritize(tool.name)
    # 外部能力（P5）：MCP server 的工具（data/mcp.json 決定有哪些）
    mcp_tools, _status = build_tools(base / "mcp.json", cwd=str(ROOT), log=_log)
    for tool in mcp_tools:
        registry.register(tool)
    return media


# ── 客人 ─────────────────────────────────────────────────────────────────────
def restore_guests(guest_store, *, principals, social, stances, entities) -> None:
    """核可過的客人：重開機時把人、跟主人的關係邊、三個角色的立場補回來。"""
    for request in guest_store.accounts():
        # 主人不是客人：舊版「主人輪替自己」會把自己寫進 guests.json，建 owner→owner 關係會拋例外、
        # 整個服務起不來（2026-10-31 複檢實測）⇒ 跳過並提示，不擋開機
        existing = principals.get(request.principal_id)
        if request.principal_id == "owner" or (existing is not None and getattr(existing, "owner", False)):
            print(f"  guests: 略過 guests.json 裡的主人本人（{request.principal_id}）", flush=True)
            continue
        if principals.get(request.principal_id) is None:
            principals.register(Principal(request.principal_id, request.display_name,
                                          role="guest", discord_id=request.discord_id))
        if request.final is not None:
            social.add_relation("owner", request.principal_id, request.final.kind,
                                label=request.final.label, trust=request.trust)
        kind = request.final.kind if request.final is not None else "other"
        for entity in entities.all():
            stances.ensure_relation(entity.id, request.principal_id, kind)



# ── 維護 ─────────────────────────────────────────────────────────────────────
def start_maintenance(base, *, memory, raw_log, entity_ids, entity_names, vectors=None):
    """每日資料快照（她的記憶只有一份，先加保險）＋背景提煉（`BLUEAGENT_DISTILL=1` 才開）＋自傳式記憶重建。"""
    from agent.brain.runtime.ops.backup import DataBackup, start_worker as start_backup
    backup = DataBackup(base)
    if str(os.environ.get("BLUEAGENT_BACKUP") or "1").strip().lower() \
            not in {"0", "false", "no", "off"}:
        start_backup(backup,
                     interval_s=float(os.environ.get("BLUEAGENT_BACKUP_INTERVAL_S") or 86400))
    _start_self_model_worker(base, memory=memory, entity_ids=entity_ids)
    # 背景提煉（owner 2026-10-23「走 P2」）：session 過濾 → DeepSeek 抽事實 → 門檻入檔 → 履歷
    distiller = None
    if str(os.environ.get("BLUEAGENT_DISTILL") or "0").strip().lower() in {"1", "true", "yes", "on"}:
        from agent.brain.runtime.distill import Distiller, start_worker
        distiller = Distiller(memory=memory, raw_log=raw_log, entities=entity_ids,
                              names=entity_names, vectors=vectors)
        start_worker(distiller, interval_s=float(
            os.environ.get("BLUEAGENT_DISTILL_INTERVAL_S") or 600))
    return backup, distiller

def seal_on_supersede(threads, archive) -> None:
    """換世代時先把舊線的尾巴封片（見 `ThreadStore.on_supersede`）；結束時刻＝舊線最後活動時間。"""
    from agent.brain.domain.episode import CutReason
    seal = getattr(archive, "seal", None)
    if seal is None:
        return
    threads.on_supersede(lambda previous: seal(previous, float(previous.last_active_at or 0.0), CutReason.IDLE))


def start_thread_reaper(threads, archive, *, log=print):
    """線的回收器（E4，2026-11-17；見 `runtime/ops/thread_reaper.py`）：開機清一次，之後每 10 分鐘。

    `BLUEAGENT_THREAD_REAP=0` 關掉；門檻可調：`BLUEAGENT_THREAD_SUPERSEDED_S`（舊世代，預設 6 小時）、
    `BLUEAGENT_THREAD_STALE_S`（當世代，預設 30 天）。
    """
    from agent.brain.runtime.ops.thread_reaper import ThreadReaper, start_worker
    superseded = os.environ.get("BLUEAGENT_THREAD_SUPERSEDED_S") or ""
    stale = os.environ.get("BLUEAGENT_THREAD_STALE_S") or ""
    reaper = ThreadReaper(
        threads, seal=getattr(archive, "seal", None), log=lambda message: log(f"  {message}", flush=True),
        superseded_after=float(superseded) if superseded.strip() else None,
        stale_after=float(stale) if stale.strip() else None)
    if str(os.environ.get("BLUEAGENT_THREAD_REAP") or "1").strip().lower() not in {"0", "false", "no", "off"}:
        start_worker(reaper, interval_s=float(os.environ.get("BLUEAGENT_THREAD_REAP_INTERVAL_S") or 600))
    return reaper


def _start_self_model_worker(base, *, memory, entity_ids) -> None:
    """自傳式記憶（提案⑤）每天重建一次：確定性、不叫模型，壞了只印一行。"""
    import threading
    from agent.brain.runtime.inner import self_facts, self_model

    if str(os.environ.get("BLUEAGENT_SELF_MODEL") or "1").strip().lower() \
            in {"0", "false", "no", "off"}:
        return

    def loop() -> None:
        interval = float(os.environ.get("BLUEAGENT_SELF_MODEL_INTERVAL_S") or 86400)
        while True:
            try:
                from agent.brain.runtime.memory.memory_search import memory_db_path
                db = memory_db_path(memory)
                for entity in entity_ids:
                    self_model.rebuild(base, entity, db_path=db)
                    self_facts.forget(entity)
                print(f"  self-model: 重建完（{len(entity_ids)} 個角色）", flush=True)
            except Exception as exc:                       # noqa: BLE001 - 壞了不擋維護
                print(f"  self-model: 重建失敗（{type(exc).__name__}: {exc}）", flush=True)
            time.sleep(max(600.0, interval))

    threading.Thread(target=loop, name="self-model", daemon=True).start()


def build_timeline(activity_log):
    """時間感的容器（`runtime/inner/timeframe.Timeline`）；archive／heartbeats 由 bootstrap 晚一步補。"""
    from agent.brain.runtime.inner.timeframe import Timeline
    return Timeline(activity_log=activity_log)


def build_reminders(base):
    """「幾跳之後提某件事」的預約倉（`runtime/inner/reminders.py`）。"""
    from agent.brain.runtime.inner.reminders import ReminderStore
    return ReminderStore(base / "reminders")


def build_beats(base):
    """心跳留痕（`runtime/inner/beats.py`）：每一跳一行，事後能回指「第 N 跳」。"""
    from agent.brain.runtime.inner.beats import BeatLog
    return BeatLog(base / "beats")

def build_pending_delivery(base):
    """主人要她交付、她還沒交的東西（`runtime/inner/pending_delivery.py`，owner 2026-11-08）。"""
    from agent.brain.runtime.inner.pending_delivery import PendingDeliveryStore
    ttl = float(os.environ.get("BLUEAGENT_PENDING_DELIVERY_TTL_DAYS") or 3)
    return PendingDeliveryStore(base / "pending_delivery", ttl_days=ttl)


def build_stores(base, entities):
    """現況倉／任務台帳／未渡しの依頼——`build()` 只要一行（單檔行數有守門）。

    · 現況倉：她「此刻」在做的那件事（pipeine 的現況段與 LifeLoop 共用同一份檔案）
    · 任務台帳（owner 2026-11-03）：`/api/tasks` 與 LifeLoop 吃**同一份**實例；
      她動台帳走外置 MCP（`tools/mcp_tasks_server.py`，另一個行程），跨行程一致性在 TaskLedger 裡。
    · 未渡しの依頼（owner 2026-11-08）：回話與心跳都看得到，真的交付才清。
    """
    from agent.brain.media.files import FileStore
    from agent.brain.runtime.activity_store import ActivityStore
    from agent.brain.runtime.inner.task_ledger import TaskLedger
    activity_store = ActivityStore(base / "activities")
    ledgers = {entity.id: TaskLedger(base / "tasks", entity.id) for entity in entities.all()}
    return activity_store, ledgers, build_pending_delivery(base), FileStore(base / "files")

def register_remind_tool(registry, reminders, heartbeats) -> None:
    """`remind.in`：她替自己排「幾跳之後／幾分鐘後／幾點」要提某件事（owner 2026-10-27）。"""
    from agent.brain.runtime.tools.remind import RemindTool

    def beat_state(entity: str) -> dict:
        beat = heartbeats.get(entity)
        return beat.snapshot() if beat is not None else {}

    registry.register(RemindTool(reminders, beats=beat_state))


__all__ = ["CONVERSATIONAL_KINDS", "Search", "SideCalls", "build_memory", "build_reminders",
           "build_search", "build_side_calls", "build_timeline", "make_public_sink",
           "register_remind_tool", "register_tools", "restore_guests", "start_maintenance"]
