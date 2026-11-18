"""她自己的「正在做的事」：串行、可暂停、可检查点、可恢复。

一个人不是多线程的。她一次只推进一件事，被叫住就停在安全边界、记下自己走到哪，
回头从断点继续，而不是从头再来。这个模型服务于：

    · 她自己的活动（TRPG 角色卡、出门、整理东西）——无主源；
    · 长任务/多步计划；
    · 进程重启后的恢复（RUNNING 的步骤按「可能做到一半」重试）。

设计要点：
    Activity  一件事：有序的 Step 列表 + cursor（当前步）+ 状态 + 检查点时间。
    cursor    断点：0..len(steps)；已完成的步不再重跑。
    checkpoint 每次状态变化都写盘（`ActivityStore` 原子写）。
    RUNNING   从磁盘读回时一律降级为 PAUSED，并把 RUNNING 的步骤退回 PENDING（重试当前步）。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ActivityStatus(str, Enum):
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class ActivityStep:
    id: str
    title: str
    status: StepStatus = StepStatus.PENDING
    started_at: float = 0.0
    ended_at: float = 0.0
    note: str = ""
    #: 這一步的紀錄是 `private`（只有她能回溯）還是 `public`（可被別人檢索）。
    visibility: str = "public"

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "status": self.status.value,
                "started_at": round(self.started_at, 3),
                "ended_at": round(self.ended_at, 3), "note": self.note,
                "visibility": self.visibility}

    @classmethod
    def from_dict(cls, raw: dict) -> "ActivityStep":
        try:
            status = StepStatus(str(raw.get("status") or "pending"))
        except ValueError:
            status = StepStatus.PENDING
        return cls(id=str(raw.get("id") or secrets.token_hex(4)),
                   title=str(raw.get("title") or "一步"),
                   status=status, started_at=float(raw.get("started_at") or 0.0),
                   ended_at=float(raw.get("ended_at") or 0.0),
                   note=str(raw.get("note") or ""),
                   visibility=str(raw.get("visibility") or "public"))


@dataclass
class Activity:
    id: str
    entity: str
    title: str
    steps: list[ActivityStep]
    cursor: int = 0
    status: ActivityStatus = ActivityStatus.READY
    kind: str = "task"
    ownerless: bool = True
    created_at: float = 0.0
    updated_at: float = 0.0
    checkpoint_at: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    # ── 建構 ────────────────────────────────────────────────────────────────────
    @classmethod
    def create(cls, entity: str, title: str, step_titles: list[str], *,
               kind: str = "task", ownerless: bool = True,
               at: float | None = None,
               visibilities: list[str] | None = None) -> "Activity":
        now = float(at if at is not None else time.time())
        marks = list(visibilities or [])
        return cls(id=f"act_{secrets.token_hex(6)}", entity=str(entity), title=str(title),
                   steps=[ActivityStep(id=f"s_{secrets.token_hex(3)}", title=str(name),
                                       visibility=(marks[index] if index < len(marks)
                                                   else "public"))
                          for index, name in enumerate(step_titles)],
                   kind=str(kind), ownerless=bool(ownerless), created_at=now,
                   updated_at=now, checkpoint_at=now)

    # ── 進度 ────────────────────────────────────────────────────────────────────
    @property
    def total(self) -> int:
        return len(self.steps)

    @property
    def current(self) -> ActivityStep | None:
        return self.steps[self.cursor] if 0 <= self.cursor < len(self.steps) else None

    @property
    def done(self) -> bool:
        return self.cursor >= len(self.steps) or self.status is ActivityStatus.DONE

    def checkpoint(self, now: float | None = None) -> None:
        at = float(now if now is not None else time.time())
        self.updated_at = at
        self.checkpoint_at = at

    def start(self, now: float | None = None) -> None:
        if self.status in {ActivityStatus.DONE, ActivityStatus.FAILED}:
            return
        self.status = ActivityStatus.RUNNING
        self.checkpoint(now)

    def pause(self, now: float | None = None) -> None:
        if self.status is ActivityStatus.RUNNING:
            self.status = ActivityStatus.PAUSED
        self.checkpoint(now)

    def resume(self, now: float | None = None) -> None:
        if self.status in {ActivityStatus.PAUSED, ActivityStatus.READY} and not self.done:
            self.status = ActivityStatus.RUNNING
        self.checkpoint(now)

    def advance(self, now: float | None = None, note: str = "") -> None:
        step = self.current
        if step is None:
            self.status = ActivityStatus.DONE
            self.checkpoint(now)
            return
        at = float(now if now is not None else time.time())
        step.status = StepStatus.DONE
        step.ended_at = at
        step.note = str(note or step.note)
        self.cursor += 1
        if self.cursor >= len(self.steps):
            self.status = ActivityStatus.DONE
        self.checkpoint(at)

    # ── 序列化 ──────────────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "id": self.id, "entity": self.entity, "title": self.title,
            "kind": self.kind, "ownerless": self.ownerless,
            "status": self.status.value, "cursor": self.cursor,
            "total": self.total,
            "created_at": round(self.created_at, 3),
            "updated_at": round(self.updated_at, 3),
            "checkpoint_at": round(self.checkpoint_at, 3),
            "steps": [step.to_dict() for step in self.steps],
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Activity":
        status = ActivityStatus(str(raw.get("status") or "ready"))
        steps = [ActivityStep.from_dict(item) for item in (raw.get("steps") or [])]
        meta = dict(raw.get("meta") or {})
        # 程序重啟：RUNNING 不可信；當前步退回 PENDING，讓她從這一步重試。
        if status is ActivityStatus.RUNNING:
            status = ActivityStatus.PAUSED
            meta["recovered"] = True
            for step in steps:
                if step.status is StepStatus.RUNNING:
                    step.status = StepStatus.PENDING
                    step.started_at = 0.0
        return cls(
            id=str(raw.get("id") or f"act_{secrets.token_hex(6)}"),
            entity=str(raw.get("entity") or "mira"),
            title=str(raw.get("title") or "未命名的事"),
            steps=steps, cursor=int(raw.get("cursor") or 0), status=status,
            kind=str(raw.get("kind") or "task"),
            ownerless=bool(raw.get("ownerless", True)),
            created_at=float(raw.get("created_at") or 0.0),
            updated_at=float(raw.get("updated_at") or 0.0),
            checkpoint_at=float(raw.get("checkpoint_at") or 0.0),
            meta=meta,
        )
