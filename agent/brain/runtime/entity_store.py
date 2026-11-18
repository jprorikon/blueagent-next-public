"""實體的「自身狀態」：在場、注意力、情緒。

這是「一個角色 ＝ 一個實體」帶來的第二個推論：**狀態屬於實體，不屬於連線**。

舊模型把 `soul`／`device`／`mode` 存在 `EAR_PERSONA[session]`、把 TTS 開關存在
`EAR_TTS[session]`、把情緒散在 AEM 的各個快取裡。於是「她現在心情怎樣」「她正在跟誰說話」
會隨著連線建立／斷開而丟失。新模型：

    EntityState
      ├─ presence   她現在「在」哪些源（手機、Discord、房間、桌上）——可以很多個
      ├─ attention  她此刻把注意力放在哪條線（最多一條）——所以不會精神分裂
      └─ mood       情緒／關係的即時快取（長期值仍在 affect 層）
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from agent.brain.domain.event import SourceClass


@dataclass(frozen=True, slots=True)
class Presence:
    """一個實體在某個源上的在場。`since`＝這次出現的時刻，`last_seen`＝最後一次動靜。"""

    source: str
    scene: str
    source_class: SourceClass
    since: float
    last_seen: float
    #: 這個在場是「誰」在（群場景的同一條源上，不同人可以有各自的在場記錄）。
    principal: str = ""


@dataclass
class EntityState:
    entity_id: str
    presence: dict[str, Presence] = field(default_factory=dict)
    attention: str = ""
    #: 注意力落在哪個人身上（群場景裡同一條線、不同發言人）。
    attention_principal: str = ""
    mood: dict[str, float] = field(default_factory=dict)
    updated_at: float = 0.0
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "entity": self.entity_id,
                "attention": self.attention,
                "attention_principal": self.attention_principal,
                "presence": {src: {"scene": p.scene, "class": p.source_class.label,
                                   "principal": p.principal,
                                   "since": round(p.since, 3), "last_seen": round(p.last_seen, 3)}
                             for src, p in self.presence.items()},
                "mood": dict(self.mood),
                "updated_at": round(self.updated_at, 3),
            }


class EntityStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, EntityState] = {}

    def get(self, entity_id: str) -> EntityState | None:
        with self._lock:
            return self._states.get(str(entity_id))

    def state(self, entity_id: str) -> EntityState:
        key = str(entity_id)
        with self._lock:
            state = self._states.get(key)
            if state is None:
                state = EntityState(entity_id=key)
                self._states[key] = state
            return state

    # ── 在場 ────────────────────────────────────────────────────────────────────
    def mark_presence(self, entity: str, source: str, scene: str, source_class: SourceClass,
                      at: float, principal: str = "") -> Presence:
        state = self.state(entity)
        with state.lock:
            previous = state.presence.get(source)
            presence = Presence(source=source, scene=scene, source_class=source_class,
                                since=previous.since if previous else at, last_seen=at,
                                principal=str(principal or ""))
            state.presence[source] = presence
            state.updated_at = at
            return presence

    def drop_presence(self, entity: str, source: str) -> bool:
        state = self.get(entity)
        if state is None:
            return False
        with state.lock:
            return state.presence.pop(source, None) is not None

    def presence(self, entity: str) -> tuple[Presence, ...]:
        state = self.get(entity)
        if state is None:
            return ()
        with state.lock:
            return tuple(state.presence.values())

    def drop_stale(self, now: float, *, stale_after: float = 10 * 60.0) -> list[tuple[str, str]]:
        """清掉太久沒動靜的在場（例如 App 被殺、Discord 斷線）——但**不影響記憶**。"""
        dropped: list[tuple[str, str]] = []
        with self._lock:
            states = list(self._states.values())
        for state in states:
            with state.lock:
                for source, presence in list(state.presence.items()):
                    if now - presence.last_seen >= stale_after:
                        state.presence.pop(source, None)
                        dropped.append((state.entity_id, source))
        return dropped

    # ── 注意力 ──────────────────────────────────────────────────────────────────
    def attend(self, entity: str, thread_id: str, at: float, principal: str = "") -> None:
        """把注意力移到某條線（以及某個人）。只有**非環境**輸入會呼叫它。"""
        state = self.state(entity)
        with state.lock:
            state.attention = str(thread_id)
            state.attention_principal = str(principal or "")
            state.updated_at = at

    def attention(self, entity: str) -> str:
        state = self.get(entity)
        if state is None:
            return ""
        with state.lock:
            return state.attention

    def attention_principal(self, entity: str) -> str:
        state = self.get(entity)
        if state is None:
            return ""
        with state.lock:
            return state.attention_principal

    # ── 情緒快取 ────────────────────────────────────────────────────────────────
    def set_mood(self, entity: str, key: str, value: float, at: float) -> None:
        state = self.state(entity)
        with state.lock:
            state.mood[str(key)] = float(value)
            state.updated_at = at

    def snapshot(self) -> list[dict]:
        with self._lock:
            states = list(self._states.values())
        return [state.snapshot() for state in states]
