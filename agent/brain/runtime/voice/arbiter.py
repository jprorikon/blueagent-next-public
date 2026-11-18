"""發言權仲裁：**一個實體只有一張嘴**。

這是「一個角色 ＝ 一個實體」最直接的推論：她可以同時在多處**在場**、多處**想事情**，
但同一瞬間只可能在一處**說話**。所以仲裁鍵是 `entity`，不是場景、不是 session。

規則：
  · 同一個實體，同時只有一個 holder。
  · 優先級數字更小（更實時）的來源可以**搶佔**：正在說話的那一輪會在最近的階段邊界收手
    （令牌 + epoch 雙重守衛），新的一輪拿到發言權。
  · 沒拿到的人可以等（`timeout`）或直接放棄（回去當文字／環境回合）。

這解決的是舊模型裡「手機 SSE 和 Discord 語音各自往自己的連接寫 speech 幀」的串話問題。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Floor:
    holder: str
    priority: int
    since: float
    principal: str = ""


class FloorArbiter:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._floor: dict[str, Floor] = {}

    def holder(self, entity: str) -> str | None:
        with self._cond:
            floor = self._floor.get(str(entity))
            return floor.holder if floor else None

    def current(self, entity: str) -> Floor | None:
        with self._cond:
            return self._floor.get(str(entity))

    def acquire(self, entity: str, holder: str, *, priority: int = 10,
                principal: str = "", timeout: float | None = None) -> bool:
        """拿發言權。`priority` 更小＝更有資格插話；等不到就回 False（呼叫端自己降級成文字）。"""
        key = str(entity)
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._cond:
            while True:
                floor = self._floor.get(key)
                if floor is None or floor.holder == holder:
                    self._floor[key] = Floor(holder=holder, priority=int(priority),
                                             since=time.monotonic(), principal=str(principal or ""))
                    self._cond.notify_all()
                    return True
                if int(priority) < floor.priority:
                    # 更實時的來源插話：直接把發言權交出去（舊 holder 會在階段邊界收手）
                    self._floor[key] = Floor(holder=holder, priority=int(priority),
                                             since=time.monotonic(), principal=str(principal or ""))
                    self._cond.notify_all()
                    return True
                if deadline is None:
                    self._cond.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)

    def release(self, entity: str, holder: str) -> bool:
        key = str(entity)
        with self._cond:
            floor = self._floor.get(key)
            if floor is None or floor.holder != holder:
                return False
            self._floor.pop(key, None)
            self._cond.notify_all()
            return True

    def release_entity(self, entity: str, *, preserve_holder: str = "") -> str:
        """**打斷**：收回回合發言權；回傳原本的 holder（沒有／保留＝空字串）。

        只給「人開口打斷她」用（barge-in）：主人插話時，她那一輪的 floor 是他給的，
        不需要先問她是誰在說——收回之後她會在最近的階段邊界收手。
        通話 session 的 floor 由 `preserve_holder` 保留到掛斷，避免下一回合繞過仲裁。
        檢查與釋放必須在同一個鎖內，不能給其他來源插入的窗口。
        """
        key = str(entity)
        with self._cond:
            floor = self._floor.get(key)
            if floor is None or (preserve_holder and floor.holder == preserve_holder):
                return ""
            self._floor.pop(key, None)
            self._cond.notify_all()
            return floor.holder

    def holds(self, entity: str, holder: str) -> bool:
        with self._cond:
            floor = self._floor.get(str(entity))
            return bool(floor and floor.holder == holder)

    def snapshot(self) -> dict:
        with self._cond:
            return {entity: {"holder": floor.holder, "priority": floor.priority,
                             "since": floor.since, "principal": floor.principal}
                    for entity, floor in self._floor.items()}
