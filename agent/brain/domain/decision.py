"""路由決策：**一次分流的全部結果**，唯一能被日誌／面板解釋的東西。

`RouteDecision` 是「這一件事該由哪個實體、哪條線、哪條管線、什麼併發語義來處理」的
不可變快照。它同時是驗收依據：只要 `to_log()` 一行就能回答「她為什麼回這邊」。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agent.brain.domain.event import SourceClass
from agent.brain.domain.ids import ThreadKey
from agent.brain.domain.thread import MemoryScope


class Engagement(str, Enum):
    """她要不要接這句話。

    REPLY   ：衝她來的（DM、@她、叫她名字、回覆她）→ 跑完整管道並回覆。
    OBSERVE ：別人之間的對話 → 記進時間線、更新在場，但**不跑腦、不出聲**。
    IGNORE  ：噪音／自己人的回執 → 丟掉。
    """

    REPLY = "reply"
    SILENT = "silent"      # 旁聽/暗中記帳：不跑腦、不出聲（原 OBSERVE）
    OBSERVE = "observe"    # 兼容別名；新程式用 SILENT
    REACT = "react"        # 貼一個表情表示記下了（零噪音確認）
    YIELD = "yield"        # 先禮讓 8–15s，沒人接再補位
    IGNORE = "ignore"


class DispatchMode(str, Enum):
    ATTACH = "attach"        # 直接開始新一輪
    INTERRUPT = "interrupt"  # 搶佔當前那一輪（實時源）
    QUEUE = "queue"          # 等她手上這一輪結束（互動源）
    DROP = "drop"            # 丟掉／合併（環境源的重複輸入）


@dataclass(frozen=True, slots=True)
class RouteDecision:
    thread_id: str
    key: ThreadKey
    pipeline: str
    lane: SourceClass
    priority: int
    mode: DispatchMode
    scope: MemoryScope
    deadline_ms: int | None
    reason: str
    created: bool
    at: float
    #: 距離這條線上一次活動多久（秒）。路由為了連續性會先 touch，所以切片要看這個值。
    idle_s: float = 0.0
    #: 這次輸入從哪個源進來（用於在場登記與日誌；不是 thread 的一部分）。
    source: str = ""
    #: 這次說話的人（群場景裡同一條線會有多個）。
    principal: str = ""
    #: 她要不要接話（reply/observe/ignore）。
    engage: Engagement = Engagement.REPLY
    #: 這句在說給誰（`entity` / `group` / principal / 空）。
    addressed_to: str = ""
    #: 注意力閘門的解釋（explicit / focus_lease / score:yield …）。
    engage_reason: str = ""
    #: YIELD 的禮讓窗口（毫秒）；其他動作＝0。
    yield_ms: int = 0

    @property
    def entity(self) -> str:
        return self.key.entity

    def to_log(self) -> str:
        return (f"route: thread={self.thread_id} entity={self.key.entity} key={self.key.to_log()} "
                f"pipeline={self.pipeline} lane={self.lane.label} prio={self.priority} "
                f"mode={self.mode.value} engage={self.engage.value} "
                f"engage_reason={self.engage_reason} "
                f"created={int(self.created)} {self.reason}")

    def to_dict(self) -> dict:
        return {
            "thread": self.thread_id,
            "entity": self.entity,
            "key": self.key.to_log(),
            "pipeline": self.pipeline,
            "lane": self.lane.label,
            "priority": self.priority,
            "mode": self.mode.value,
            "deadline_ms": self.deadline_ms,
            "reason": self.reason,
            "created": self.created,
            "at": self.at,
            "idle_s": self.idle_s,
            "source": self.source,
            "principal": self.principal,
            "engage": self.engage.value,
            "addressed_to": self.addressed_to,
            "engage_reason": self.engage_reason,
            "yield_ms": self.yield_ms,
            "scope": self.scope.to_dict(),
        }
