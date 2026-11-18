"""輸入事件與輸出幀：**源只說「發生了什麼」，不說「該怎麼處理」**。

這裡刻意不認識 HTTP、不認識 `deps`、不認識任何業務模組。一個 `IngressEvent` 是
「某個源、對某個實體、在某一刻收到的一個輸入」；一個 `OutFrame` 是「某一條線的某一輪
要送回去的一幀」。

`source_class` 是併發語義的唯一定義（使用者 2026-10-21 拍板）：

  · REALTIME    實時源（麥克風／Discord 語音／當面）→ **並行**、可搶佔、能拿發言權
  · INTERACTIVE 互動源（手機打字／Discord 文字）    → **排隊**（同一條線串行）
  · AMBIENT     環境源（TRPG 桌／棋局／提醒／背景） → **排隊**，最低優先級、通常不出聲

源的分類不是「哪個 app」，而是「這個入口的即時性」——同一個 app 的語音與打字可以分屬兩類。
`entity` 是這條輸入指向哪個角色實體；源可以綁定預設實體，事件裡也可以覆蓋。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum, IntEnum
from typing import Any, Mapping


class InputKind(str, Enum):
    TEXT = "text"
    AUDIO = "audio"
    IMAGE = "image"
    SIGNAL = "signal"      # 在場／已讀／心跳／工具回執之類，不產生回合
    PRESENCE = "presence"


class Origin(str, Enum):
    """這條輸入是「某個人帶來的」還是「她自己發起的」。

    OWNED：DM、手機、Discord 頻道——有一個說話的人，記憶按人／關係隔離。
    SELF ：TRPG 桌、她自己的行程、背景活動——**沒有主人**；線的錨是 `self:<scene>`，
           記憶是「她自己的經歷」，不屬於任何 principal。
    """

    OWNED = "owned"
    SELF = "self"


class ConversationMode(str, Enum):
    """這條源上的對話是「一對一」還是「群場景」。

    DIRECT：每個人各自一條線（手機、Discord 私聊）。
    GROUP ：所有人共用一條線（Discord 頻道、TRPG 桌）——不共用就會在同一個房間裡
            同時進行兩段對話，這是多 principal 最容易踩的坑。
    """

    DIRECT = "direct"
    GROUP = "group"


class SourceClass(IntEnum):
    """數值愈小＝優先級愈高（與 `priority` 同向，調度器直接比較）。"""

    REALTIME = 0
    INTERACTIVE = 1
    AMBIENT = 2

    @property
    def label(self) -> str:
        return self.name.lower()


class FrameKind(str, Enum):
    READ = "read"
    THINKING = "thinking"
    TOOL = "tool"
    APPROVAL = "approval"
    DELTA = "delta"
    SPEECH = "speech"
    QUEUED = "queued"
    INTERRUPTED = "interrupted"
    DONE = "done"
    ERROR = "error"
    PRESENCE = "presence"
    MEMORY = "memory"          # 記憶沈澱／壓縮的可見回執（預設靜默，除錯時才開）


@dataclass(frozen=True, slots=True)
class IngressEvent:
    """一個源送進來的一件事。`payload` 保留原始內容，管線各階段自己解釋。"""

    #: 這次是誰在說話（源在認證後解析；不信任 payload 自報）。
    principal: str
    entity: str
    source: str
    kind: InputKind
    at: float
    source_class: SourceClass
    #: 這條輸入的歸屬（見 `Origin`）。
    origin: Origin = Origin.OWNED
    #: 這個源能不能貼 reaction（零噪音確認）。由 SourceCaps 帶上來。
    reactions: bool = False
    #: 線的錨：空＝用 principal；群場景由源給 `group:<scene>`；無主源給 `self:<scene>`。
    scope: str = ""
    #: 這句是**說給誰**的：`entity` / `group` / 某個 principal / 空＝未知。
    #: 與 `principal`（誰說的）是兩件事——Discord 的 mention/reply 就映射到這裡。
    addressed_to: str = ""
    #: 這一輪**在場的人**（房間名冊）。記憶檢索用它來判斷可見域；
    #: 空＝只有說話的人在場（一對一）。
    participants: tuple[str, ...] = ()
    scene: str = ""
    thread_hint: str = ""
    pipeline_hint: str = ""
    #: 分散式追蹤 ID（沿用舊契約的 trace，沒有就空）。
    trace_id: str = ""
    #: 幂等鍵：`source:id` 由 runtime 拼；空＝不去重（語音碎片通常沒有穩定的外部 id）。
    idempotency_key: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    trace: Mapping[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return str(self.payload.get("text") or "").strip()

    def with_text(self, text: str) -> "IngressEvent":
        payload = dict(self.payload)
        payload["text"] = text
        return replace(self, payload=payload)

    def with_kind(self, kind: InputKind) -> "IngressEvent":
        return replace(self, kind=kind)

    def to_log(self) -> str:
        trace = f" trace={self.trace_id}" if self.trace_id else ""
        anchor = f" scope={self.scope}" if self.scope else ""
        who = self.principal or ("她自己" if self.origin is Origin.SELF else "?")
        return (f"entity={self.entity} who={who} src={self.source} "
                f"kind={self.kind.value} cls={self.source_class.label} "
                f"scene={self.scene or '-'}{anchor}{trace} text={self.text[:24]!r}")


@dataclass(frozen=True, slots=True)
class OutFrame:
    """送給某個源的一幀。`epoch` 是防串話的核心：過期的回合一律丟。"""

    kind: FrameKind | str
    payload: Mapping[str, Any]
    thread_id: str
    turn_id: str
    epoch: int
    final: bool = False
    #: 這幀要回給誰（群聊裡 @ 對方；DM 可忽略）。
    reply_to: str = ""
    #: 回寫時要不要真的 @ 出來（由源的 egress 決定）。
    mention: bool = False

    def to_log(self) -> str:
        kind = self.kind.value if isinstance(self.kind, FrameKind) else str(self.kind)
        return f"frame={kind} thread={self.thread_id} turn={self.turn_id} epoch={self.epoch}"
