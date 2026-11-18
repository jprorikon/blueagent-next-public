"""管線協議：一輪 ＝ 一條 `Stage` 序列，**階段是插件，管線是宣告**。

    Envelope   這一輪的可變資料（事件、決策、線、文字、附件、meta）
    TurnContext 這一輪的輸出與取消：`emit()` 已帶 epoch 守衛，過期回合一律丟
    Stage      純插件：`run(env, ctx) -> StageOutcome | None`
    Pipeline   把有序 stage 串起來；只負責「跑、檢查取消、收尾」
    PipelineRegistry 名稱 → 管線（組合根註冊，router 只看 id）

硬規則：**stage 不自己起執行緒、不 import `deps`、不直接寫 socket**。
要並行就在 stage 內用 ports 提供的併發原語；要輸出就走 `ctx.emit()`。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, Sequence

from agent.brain.domain.decision import RouteDecision
from agent.brain.domain.event import FrameKind, IngressEvent, OutFrame
from agent.brain.domain.thread import Thread, TurnRecord


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """一輪送進模型的 token 預算分配（JIT 装配，而不是「取最近 N 條」）。

    四個分區借自另一份 spec 的思路，但數字按 blueagent 的體感重排：

        system  身份／人格／能力／SOP
        active  當前目標／未完成的事（episode.open_loops）
        recent  跨源近期時間線（transcript 滾動窗）
        recall  長期記憶／片段摘要（Hindsight）

    `recent` 拿餘數，所以四段加起來一定等於 total，不會因為取整丟 token。
    """

    total: int = 16000
    system: float = 0.15
    active: float = 0.25
    recent: float = 0.35
    recall: float = 0.25

    def alloc(self) -> dict[str, int]:
        total = max(0, int(self.total))

        def part(ratio: float) -> int:
            return max(0, int(total * max(0.0, float(ratio))))

        system = part(self.system)
        active = part(self.active)
        recall = part(self.recall)
        return {"system": system, "active": active,
                "recent": max(0, total - system - active - recall), "recall": recall}


#: 場景預設：打字有時間讀長上下文；語音只有幾秒，必須更小、身份佔比更高。
DEFAULT_CONTEXT_BUDGET = ContextBudget(total=16000)
VOICE_CONTEXT_BUDGET = ContextBudget(total=6000, system=0.20, active=0.20,
                                     recent=0.35, recall=0.25)


@dataclass(slots=True)
class Part:
    """一輪裡的一塊內容（文字／音訊／圖片／產物）。階段可以讀也可以追加。"""

    kind: str
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Envelope:
    event: IngressEvent
    decision: RouteDecision
    thread: Thread
    turn_id: str
    text: str
    parts: list[Part] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def add_part(self, kind: str, text: str = "", **data: Any) -> Part:
        part = Part(kind=kind, text=text, data=dict(data))
        self.parts.append(part)
        return part

    def text_of(self, kind: str) -> str:
        return "\n".join(p.text for p in self.parts if p.kind == kind and p.text)


class CancelSignal(Protocol):
    @property
    def cancelled(self) -> bool:
        ...


class StageOutcome(str, Enum):
    CONTINUE = "continue"
    STOP = "stop"


class Stage(Protocol):
    name: str

    def run(self, env: Envelope, ctx: "TurnContext") -> StageOutcome | None:
        ...


def _noop(_: str) -> None:
    return None


@dataclass(slots=True)
class TurnContext:
    """一輪的輸出上下文。

    `emit()` 的三道守衛（缺一不可）：
      ① 這一輪已被取消（搶佔／使用者 /stop）
      ② 這條線的 epoch 已經往前走（有更新的回合開始了）
      ③ sink 拋錯（某個源斷了）⇒ 吞掉，不能讓整輪陪葬
    """

    thread: Thread
    turn_id: str
    epoch: int
    cancel: CancelSignal
    sink: Callable[[OutFrame], None]
    ports: Any = None
    source: str = ""
    #: 這一輪在跟誰說話（回寫時要 @ 的人）。
    principal: str = ""
    log: Callable[[str], None] = _noop

    def stale(self) -> bool:
        return self.cancel.cancelled or self.thread.epoch != self.epoch

    def emit(self, kind: FrameKind | str, payload: Mapping[str, Any] | None = None,
             *, final: bool = False) -> bool:
        if self.stale():
            return False
        frame = OutFrame(kind=kind, payload=dict(payload or {}), thread_id=self.thread.id,
                         turn_id=self.turn_id, epoch=self.epoch, final=final,
                         reply_to=self.principal)
        try:
            self.sink(frame)
        except Exception as exc:                   # noqa: BLE001 - 斷線的源不該炸整輪
            self.log(f"emit: sink 失敗（{type(exc).__name__}: {str(exc)[:80]}）{frame.to_log()}")
        return True

    def record(self, role: str, text: str) -> None:
        """把一輪寫進 transcript／segment。使用者輪由 runtime 寫，助手輪由管線寫。"""
        if not text:
            return
        with self.thread.lock:
            self.thread.record(TurnRecord(turn_id=self.turn_id, role=role, text=text,
                                          source=self.source, at=time.time()))

    def mark(self, name: str) -> None:
        """階段打點（只進 telemetry，不發給端末）。

        `ctx.sink` 是 `FrameSink.deliver` 的 bound method ⇒ 用 `__self__` 拿到 sink 本身。
        拿不到就靜默跳過（換 sink 實作時打點消失，但不能影響回話）。
        """
        marker = getattr(getattr(self.sink, "__self__", None), "mark", None)
        if callable(marker):
            try:
                marker(str(name))
            except Exception as exc:                   # noqa: BLE001 - 打點不能擋回話
                self.log(f"mark: {name} 打點失敗（{type(exc).__name__}）")


@dataclass(slots=True)
class Pipeline:
    id: str
    stages: Sequence[Stage]

    def run(self, env: Envelope, ctx: TurnContext) -> None:
        for stage in self.stages:
            if ctx.cancel.cancelled:
                ctx.emit(FrameKind.INTERRUPTED, {"at": stage.name, "reason": "cancelled"})
                return
            if ctx.thread.epoch != ctx.epoch:
                ctx.emit(FrameKind.INTERRUPTED, {"at": stage.name, "reason": "epoch"})
                return
            outcome = stage.run(env, ctx)
            if outcome is StageOutcome.STOP:
                return


class PipelineRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, Pipeline] = {}

    def register(self, pipeline: Pipeline, *, replace: bool = False) -> Pipeline:
        with self._lock:
            if pipeline.id in self._items and not replace:
                raise ValueError(f"pipeline already registered: {pipeline.id}")
            self._items[pipeline.id] = pipeline
            return pipeline

    def get(self, pipeline_id: str) -> Pipeline | None:
        with self._lock:
            return self._items.get(str(pipeline_id))

    def require(self, pipeline_id: str) -> Pipeline:
        pipeline = self.get(pipeline_id)
        if pipeline is None:
            raise KeyError(f"unknown pipeline: {pipeline_id}")
        return pipeline

    def ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._items)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [{"id": p.id, "stages": [s.name for s in p.stages]} for p in self._items.values()]
