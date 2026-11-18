"""執行槽的兩個基本型：取消令牌與線的執行狀態。

`CancelToken` 取代散落在各模組的「世代號 + if 檢查」：搶佔、`/stop`、逾時都只是
`token.cancel()`，管線在階段邊界檢查一次即可。`Lane` 只記錄「這條線現在有沒有東西在跑、
最近那一輪的令牌是什麼」，真正的排程在 `scheduler.py`。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

from agent.brain.domain.event import SourceClass


class CancelToken:
    """可跨執行緒取消的令牌（`threading.Event` 保證記憶體可見性）。"""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(slots=True)
class Job:
    """一次待執行的回合。`done` 讓測試與呼叫端可以等它結束。"""

    thread_id: str
    lane: SourceClass
    priority: int
    fn: Callable[[CancelToken], None]
    #: 這個回合屬於哪個角色。槽位按角色算（owner 2026-10-21：mira 做 A 時 aster 要做 B）。
    entity: str = ""
    seq: int = 0
    token: CancelToken = field(default_factory=CancelToken)
    done: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None
    #: 進佇列的時刻（作廢判斷用；monotonic，不比 wall clock）。
    queued_at: float = 0.0
    #: 尚未執行就被淘汰時，讓入站層恢復自己的排隊計數。
    on_discard: Callable[[], None] | None = None

    def cancel(self) -> None:
        """協作式取消：只翻旗標，**不殺執行緒**；管線在下一個階段邊界自己停。"""
        self.token.cancel()

    def discard(self, reason: str) -> None:
        """未執行的終態：保留失敗原因、收尾呼叫端狀態，再叫醒等待者。"""
        self.error = RuntimeError(str(reason or "discarded"))
        self.token.cancel()
        try:
            if self.on_discard is not None:
                self.on_discard()
        finally:
            self.done.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self.done.wait(timeout)


@dataclass(slots=True)
class Lane:
    """一條線的執行狀態。同一條線同時只該有一個**非實時**回合在跑。"""

    thread_id: str
    priority: int
    pending: int = 0
    running: int = 0
    current: CancelToken | None = None

    def preempt(self) -> bool:
        """取消當前那一輪（若有的話）。回「是否真的打斷了東西」。"""
        token = self.current
        if token is None:
            return False
        token.cancel()
        return True
