"""Ports：新架構與舊世界的**唯一接縫**。

舊的 `deps.<name>` 查找链（server.py globals + 23 個拆檔模組，見 2026-10-20 評審 §2.2）
會靜默遮蔽同名符號，新模組不該再依賴它。所以管線只認這幾個協議：

    BrainPort    一輪怎麼跑（現在是 `brain_turn` / `/v1/runs`）
    MemoryPort   檢索與沉澱（現在是 Hindsight + devices 檔案層）
    VoicePort    合成與打斷（現在是 `voice/frames.py` 那一套）
    EgressPort   幀怎麼送到某個源（現在是各路由自己寫 response）
    Clock        時鐘（測試可以換成假的）

組合根寫 adapter 把舊函式包成 ports；管線與源都不知道舊世界長什麼樣。
這也讓「多後端／多模型」變成 BrainPort 的兩個實作，而不是散在各處的分支。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Protocol, Sequence

from agent.brain.domain.event import OutFrame, SourceClass
from agent.brain.domain.ids import ThreadKey
from agent.brain.domain.thread import MemoryScope, Thread, TurnRecord


@dataclass(frozen=True, slots=True)
class ThreadAttachment:
    """路由器需要的線程接續結果；具體存儲由組合根注入。"""

    thread: Thread
    created: bool
    previous_active: float


class ThreadStorePort(Protocol):
    """Router 對線程存儲的最小需求，不認識 runtime 的具體實作。"""

    def get(self, thread_id: str) -> Thread | None:
        ...

    def attach(self, key: ThreadKey, *, now: float, window: float,
               lane: SourceClass) -> ThreadAttachment:
        ...


@dataclass(frozen=True, slots=True)
class BrainRequest:
    thread_id: str
    entity: str
    principal: str
    text: str
    system: str = ""
    mode: str = "chat"
    want_voice: bool = False
    deadline_ms: int | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)


class BrainPort(Protocol):
    def run_turn(self, request: BrainRequest) -> Iterator[tuple[str, Mapping[str, Any]]]:
        """產出 `(事件名, 負載)`；事件名與舊 SSE 幀同名（delta/speech/done/error）。"""
        ...


class MemoryPort(Protocol):
    def recall(self, scope: MemoryScope, query: str, *, limit: int = 3,
               participants: Sequence[str] = (), stream: str = "",
               game: int | None = None) -> Sequence[str]:
        ...

    def recall_detail(self, scope: MemoryScope, query: str, *, limit: int = 3,
                      participants: Sequence[str] = (), stream: str = "",
                      game: int | None = None) -> Sequence[Any]:
        ...

    def record(self, scope: MemoryScope, turn: TurnRecord, *,
               participants: Sequence[str] = ()) -> None:
        ...


class FactSinkPort(Protocol):
    """只寫事實（活動步驟、日記這類背景產出用）：`add(text, layer=…, audience=…, …)`。"""

    def add(self, text: str, **fields: Any) -> Any:
        ...


class ProbeMemoryPort(MemoryPort, Protocol):
    """`MemoryPort` ＋ `probe`：注意力閘問「這句話有沒有記憶命中」（命中幾條，不取內容）。

    應用層的 ports 只需要這五個方法；記憶體／SQLite／Hindsight 三種後端都實作它——
    別再把型別標成某一個具體後端（2026-10-25 review：標成 InMemoryMemory 是錯的契約）。
    """

    def probe(self, scope: MemoryScope, query: str, *,
              participants: Sequence[str] = ()) -> int:
        ...


class VoicePort(Protocol):
    def speak(self, scope: MemoryScope, text: str, *, thread_id: str,
              epoch: int) -> Iterator[tuple[str, Mapping[str, Any]]]:
        ...

    def abort(self, thread_id: str) -> None:
        ...


class EgressPort(Protocol):
    def send(self, frame: OutFrame) -> None:
        ...


class Clock(Protocol):
    def now(self) -> float:
        ...


class SystemClock:
    def now(self) -> float:
        return time.time()


class NullPorts:
    """全部不接線時的替身：骨架可以先跑通形狀，再一個一個換成真 adapter。

    刻意**不**用 Mock 套件：這是產品路徑上的「安靜模式」，不是測試專用。
    """

    def recall(self, scope: MemoryScope, query: str, *, limit: int = 3,
               participants: Sequence[str] = (), stream: str = "",
               game: int | None = None) -> Sequence[str]:
        return ()

    def recall_detail(self, scope: MemoryScope, query: str, *, limit: int = 3,
                      participants: Sequence[str] = (), stream: str = "",
                      game: int | None = None) -> Sequence[Any]:
        return ()

    def record(self, scope: MemoryScope, turn: TurnRecord, *,
               participants: Sequence[str] = ()) -> None:
        return None

    def run_turn(self, request: BrainRequest) -> Iterator[tuple[str, Mapping[str, Any]]]:
        return iter(())

    def speak(self, scope: MemoryScope, text: str, *, thread_id: str,
              epoch: int) -> Iterator[tuple[str, Mapping[str, Any]]]:
        return iter(())

    def abort(self, thread_id: str) -> None:
        return None

    def send(self, frame: OutFrame) -> None:
        return None
