"""實體的「線」與記憶作用域。

`Thread` 是**運行時視圖**，不是身分：它由 `(principal, entity, scene, purpose)` 推導，
過窗自動開新的一段。真正穩定的是 `Thread.key.entity` 指向的那個角色實體。

幾個刻意設計：

  · `transcript` 是給 prompt 用的**滾動窗**（有上限）；`segment` 是當前片段的全量，
    封片（`runtime/episodes.py`）時寫成 `Episode` 再清空。這讓「自動切片」不影響模型看到的上下文。
  · `carry_summary` 是上一段被壓縮後的承接摘要——使用者換段時不會覺得「她忘了」。
  · `epoch` 每次開始新回合 +1；所有輸出幀都帶 epoch，過期的一律丟。這取代了散落在
    各模組的 `RUN_*`／`VOICE_GEN`／`BRAIN_GEN` 世代號。
  · `lock` 只保護同一條線的狀態突變（記錄、開回合、切片），不跨線阻塞。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque

from agent.brain.domain.event import SourceClass
from agent.brain.domain.ids import ThreadKey, norm


class ThreadState(str, Enum):
    ACTIVE = "active"
    WAITING = "waiting"      # 有回合在跑／排隊
    IDLE = "idle"            # 過窗但還沒封片
    CLOSED = "closed"        # 已封片、可回收


class Purpose(str, Enum):
    CHAT = "chat"
    WORK = "work"
    GAME = "game"
    TRPG = "trpg"
    AMBIENT = "ambient"


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """記憶的作用域：**實體 × 說話的人**，場景只是標籤，不是隔離牆。

    兩本帳：
      · `shared_bank()`  `blueagent-<entity>`     —— 世界觀事實（「主人怕冷」「這間房子」），
        所有 principal 都能讀；單主人階段的既有資料也放這裡。
      · `private_bank()` `blueagent-<entity>-u-<principal>` —— 這個人的私事／關係細節，
        只對他檢索、絕不給別人。

    舊模型的 bank 是 `blueagent-<device>[-<character>]`——記憶跟著裝置走，換手機就「失憶」。
    新模型把「裝置」降級成 metadata，把「人」升成一等作用域。
    """

    principal: str
    entity: str
    scene: str
    thread_id: str
    #: 無主源（TRPG、她自己的行程）：記憶屬於她自己，不屬於任何 principal。
    self_owned: bool = False

    def shared_bank(self, prefix: str = "blueagent") -> str:
        return f"{prefix}-{norm(self.entity, fallback='default')}"

    def private_bank(self, prefix: str = "blueagent") -> str:
        base = self.shared_bank(prefix)
        if self.self_owned:
            return f"{base}-self"
        who = norm(self.principal, fallback="")
        return f"{base}-u-{who}" if who else base

    def bank(self, prefix: str = "blueagent") -> str:
        """相容舊呼叫：回共享池。新程式請明確用 `shared_bank()` / `private_bank()`。"""
        return self.shared_bank(prefix)

    def banks(self, prefix: str = "blueagent") -> tuple[str, str]:
        return self.shared_bank(prefix), self.private_bank(prefix)

    def to_dict(self) -> dict:
        shared, private = self.banks()
        return {"principal": self.principal, "entity": self.entity,
                "scene": self.scene, "thread": self.thread_id,
                "self_owned": self.self_owned,
                "bank": shared, "shared_bank": shared, "private_bank": private}


def legacy_device_bank(device: str, entity: str = "", *, prefix: str = "blueagent") -> str:
    """舊 bank 名（遷移期用）：`blueagent-<device>[-<entity>]`。

    它只該出現在**匯入**路徑（把舊裝置記憶打上 `device:` 標籤併進實體池），
    不該再成為新寫入的目標。
    """
    dev = norm(device, fallback="")
    ent = norm(entity, fallback="")
    if not dev:
        return ""
    return f"{prefix}-{dev}" + (f"-{ent}" if ent else "")


@dataclass(frozen=True, slots=True)
class TurnRecord:
    turn_id: str
    role: str          # user | assistant | system
    text: str
    source: str
    at: float
    #: 這一輪是誰說的（user 輪＝principal；assistant 輪＝entity）。群場景靠它歸屬。
    speaker: str = ""
    #: 這一輪是說給誰的（`entity` / `group` / principal / 空）。群場景靠它判斷要不要接話。
    addressed_to: str = ""
    #: 這一輪的注意力動作：reply/silent/react/yield（給 UI 與歸檔用）。
    engage: str = ""

    def to_dict(self) -> dict:
        return {"turn": self.turn_id, "role": self.role, "text": self.text,
                "source": self.source, "at": self.at, "speaker": self.speaker,
                "addressed_to": self.addressed_to, "engage": self.engage}

    @classmethod
    def from_dict(cls, raw: dict) -> "TurnRecord":
        return cls(turn_id=str(raw.get("turn") or ""),
                   role=str(raw.get("role") or "user"),
                   text=str(raw.get("text") or ""),
                   source=str(raw.get("source") or ""),
                   at=float(raw.get("at") or 0.0),
                   speaker=str(raw.get("speaker") or ""),
                   addressed_to=str(raw.get("addressed_to") or ""),
                   engage=str(raw.get("engage") or ""))


@dataclass
class Thread:
    key: ThreadKey
    id: str
    lane: SourceClass
    created_at: float
    last_active_at: float
    state: ThreadState = ThreadState.ACTIVE
    epoch: int = 0
    episode_index: int = 0
    sources: set[str] = field(default_factory=set)
    #: 這條線出現過的人。群場景會有多個；一對一只有一個。
    participants: set[str] = field(default_factory=set)
    #: 給 prompt 的滾動窗（最近 N 條；超出的由壓縮階段併進 carry_summary）。
    transcript: Deque[TurnRecord] = field(default_factory=lambda: deque(maxlen=40))
    #: 當前片段的全量（封片時交給 EpisodeArchive，然後清空）。
    segment: list[TurnRecord] = field(default_factory=list)
    #: 上一段被壓縮後的承接摘要（自動切片時寫入；使用者無感）。
    carry_summary: str = ""
    inflight: str = ""
    queued: int = 0
    turns: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    @property
    def entity(self) -> str:
        return self.key.entity

    def touch(self, now: float) -> None:
        self.last_active_at = max(self.last_active_at, float(now))
        if self.state is not ThreadState.CLOSED:
            self.state = ThreadState.WAITING if (self.inflight or self.queued) else ThreadState.ACTIVE

    def record(self, turn: TurnRecord) -> None:
        """記一輪。呼叫端必須持 `self.lock`（`IngressRuntime` 會）。"""
        self.transcript.append(turn)
        self.segment.append(turn)
        self.turns += 1

    def begin_turn(self, turn_id: str, now: float) -> int:
        """開一輪：**世代 +1**，舊回合的殘留輸出全部作廢。回這一輪的 epoch。"""
        with self.lock:
            self.epoch += 1
            self.inflight = turn_id
            self.touch(now)
            return self.epoch

    def end_turn(self, turn_id: str) -> None:
        with self.lock:
            if self.inflight == turn_id:
                self.inflight = ""
                if self.state is not ThreadState.CLOSED:
                    self.state = ThreadState.WAITING if self.queued else ThreadState.ACTIVE

    def interrupt(self) -> int:
        """打斷當前那一輪（不結束線）：epoch +1，之後到達的舊幀都會被 `TurnContext.emit` 丟掉。"""
        with self.lock:
            self.epoch += 1
            self.inflight = ""
            return self.epoch

    def memory_scope(self, principal: str = "") -> MemoryScope:
        """`principal` 是**這一輪說話的人**；不給就退回線的錨（一對一時兩者相同）。

        無主線（`scope=self:*`）沒有 principal：記憶屬於她自己，bank 走 `-self`。
        """
        self_owned = self.key.scope.startswith("self")
        return MemoryScope(principal="" if self_owned else (principal or self.key.scope),
                           entity=self.key.entity, scene=self.key.scene,
                           thread_id=self.id, self_owned=self_owned)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "thread": self.id,
                "key": self.key.to_log(),
                "entity": self.entity,
                "state": self.state.value,
                "epoch": self.epoch,
                "episode": self.episode_index,
                "turns": self.turns,
                "transcript": len(self.transcript),
                "segment": len(self.segment),
                "inflight": self.inflight,
                "queued": self.queued,
                "sources": sorted(self.sources),
                "participants": sorted(self.participants),
                "ownerless": self.key.scope.startswith("self"),
                "last_active_at": round(self.last_active_at, 3),
                "carry_chars": len(self.carry_summary),
            }

    def to_store_dict(self) -> dict:
        with self.lock:
            return {
                "id": self.id, "key": self.key.to_dict(), "lane": int(self.lane),
                "created_at": self.created_at, "last_active_at": self.last_active_at,
                "state": self.state.value, "epoch": self.epoch,
                "episode_index": self.episode_index,
                "sources": sorted(self.sources),
                "participants": sorted(self.participants),
                "transcript": [turn.to_dict() for turn in self.transcript],
                "segment": [turn.to_dict() for turn in self.segment],
                "carry_summary": self.carry_summary, "turns": self.turns,
            }

    @classmethod
    def from_store_dict(cls, raw: dict) -> "Thread":
        key = ThreadKey.from_dict(raw.get("key") or {})
        try:
            lane = SourceClass(int(raw.get("lane", SourceClass.INTERACTIVE)))
        except (TypeError, ValueError):
            lane = SourceClass.INTERACTIVE
        try:
            state = ThreadState(str(raw.get("state") or "active"))
        except ValueError:
            state = ThreadState.ACTIVE
        if state is ThreadState.WAITING:
            state = ThreadState.ACTIVE
        transcript = [TurnRecord.from_dict(row) for row in raw.get("transcript") or ()
                      if isinstance(row, dict)]
        segment = [TurnRecord.from_dict(row) for row in raw.get("segment") or ()
                   if isinstance(row, dict)]
        thread = cls(
            key=key, id=str(raw.get("id") or key.with_generation(1)), lane=lane,
            created_at=float(raw.get("created_at") or 0.0),
            last_active_at=float(raw.get("last_active_at") or 0.0),
            state=state, epoch=max(0, int(raw.get("epoch") or 0)),
            episode_index=max(0, int(raw.get("episode_index") or 0)),
            sources={str(item) for item in raw.get("sources") or ()},
            participants={str(item) for item in raw.get("participants") or ()},
            transcript=deque(transcript, maxlen=40), segment=segment,
            carry_summary=str(raw.get("carry_summary") or ""),
            turns=max(0, int(raw.get("turns") or len(segment))),
        )
        return thread
