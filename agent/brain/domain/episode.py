"""自動切片：把一條線切成 `Episode`，**不需要使用者按「新會話」**。

使用者的要求（2026-10-21）：干掉「開啟新會話」這個工作，切片／歸檔／壓縮自動且無感。
所以切片不是產品動作，而是**運行時策略**：

    thread（連續體）
      └─ episode 1 ─┐  閒置 ≥ idle_gap_s    ┐
      └─ episode 2 ─┤  跨日                ├─ 自動封片 → 摘要 → 事實抽取 → 歸檔 → 檢索
      └─ episode 3 ─┤  回合數 ≥ max_turns  │
      └─ episode … ─┘  字數 ≥ max_chars    ┘

封片**不改變她記得什麼**：`Episode.summary` 與抽取出的 `facts` 進長期記憶，
`Thread.carry_summary` 承接上一段，所以使用者回來時對話仍然接得上。
只有「當前這一段要壓到多短」在變。

`cut_reason()` 是純函式（不碰 I/O、不碰時鐘），方便測試與回放。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

from agent.brain.domain.ids import ThreadKey, new_episode_id
from agent.brain.domain.thread import Thread, TurnRecord


class CutReason(str, Enum):
    IDLE = "idle"          # 隔了很久沒說話
    DAY = "day"            # 跨日
    TURNS = "turns"        # 回合數到頂
    CHARS = "chars"        # 字數到頂
    TOPIC = "topic"        # 話題切換（由可選的分類器判定）
    EXPLICIT = "explicit"  # 內部呼叫（測試／遷移），不是使用者動作


@dataclass(frozen=True, slots=True)
class SegmentPolicy:
    """切片策略。預設值偏「長」：陪伴體感上，頻繁切段會讓她顯得健忘。"""

    idle_gap_s: float = 20 * 60.0
    max_turns: int = 40
    max_chars: int = 12000
    day_boundary: bool = True
    topic_min_turns: int = 8


@dataclass(frozen=True, slots=True)
class Episode:
    """一段已封存的對話。它是歸檔/壓縮/檢索的單位，不是使用者可見的「會話」。"""

    id: str
    key: ThreadKey
    thread_id: str
    index: int
    reason: CutReason
    started_at: float
    ended_at: float
    turns: tuple[TurnRecord, ...]
    summary: str = ""
    facts: tuple[str, ...] = ()
    open_loops: tuple[str, ...] = ()

    @property
    def entity(self) -> str:
        return self.key.entity

    @property
    def chars(self) -> int:
        return sum(len(t.text) for t in self.turns)

    def digest(self) -> str:
        return (f"ep={self.id} entity={self.entity} idx={self.index} reason={self.reason.value} "
                f"turns={len(self.turns)} chars={self.chars} "
                f"range={time.strftime('%m-%d %H:%M', time.localtime(self.started_at))}"
                f"→{time.strftime('%H:%M', time.localtime(self.ended_at))}")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "thread": self.thread_id,
            "key": self.key.to_log(),
            "key_parts": self.key.to_dict(),
            "entity": self.entity,
            "index": self.index,
            "reason": self.reason.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "turns": [turn.to_dict() for turn in self.turns],
            "summary": self.summary,
            "facts": list(self.facts),
            "open_loops": list(self.open_loops),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Episode":
        key_raw = raw.get("key_parts")
        key = (ThreadKey.from_dict(key_raw) if isinstance(key_raw, dict)
               else ThreadKey.from_log(str(raw.get("key") or "")))
        try:
            reason = CutReason(str(raw.get("reason") or "explicit"))
        except ValueError:
            reason = CutReason.EXPLICIT
        turns = [TurnRecord.from_dict(row) for row in raw.get("turns") or ()
                 if isinstance(row, dict)]
        return cls(
            id=str(raw.get("id") or ""),
            key=key,
            thread_id=str(raw.get("thread") or ""),
            index=max(0, int(raw.get("index") or 0)),
            reason=reason,
            started_at=float(raw.get("started_at") or 0.0),
            ended_at=float(raw.get("ended_at") or 0.0),
            turns=tuple(turns),
            summary=str(raw.get("summary") or ""),
            facts=tuple(str(item) for item in raw.get("facts") or ()),
            open_loops=tuple(str(item) for item in raw.get("open_loops") or ()),
        )


def segment_chars(turns: list[TurnRecord]) -> int:
    return sum(len(t.text) for t in turns)


def _day(ts: float) -> tuple[int, int, int]:
    return time.localtime(ts)[:3]


def cut_reason(thread: Thread, now: float, *, incoming: str = "",
               policy: SegmentPolicy = SegmentPolicy(), topic_changed: bool = False,
               last_active: float | None = None) -> CutReason | None:
    """要不要在這一輪**之前**封掉上一段？純函式。

    `last_active` 由呼叫端傳入「真正的上一次對話時刻」：router 為了維持連續性會先
    `touch()` 目前這條線，若直接讀 `thread.last_active_at` 就永遠看不到閒置間隔。
    """
    if not thread.segment:
        return None
    anchor = float(last_active if last_active is not None else thread.last_active_at)
    if now - anchor >= policy.idle_gap_s:
        return CutReason.IDLE
    if policy.day_boundary and _day(anchor) != _day(now):
        return CutReason.DAY
    if len(thread.segment) >= policy.max_turns:
        return CutReason.TURNS
    if segment_chars(thread.segment) + len(incoming) > policy.max_chars:
        return CutReason.CHARS
    if topic_changed and len(thread.segment) >= policy.topic_min_turns:
        return CutReason.TOPIC
    return None


def seal(thread: Thread, now: float, reason: CutReason) -> Episode | None:
    """把當前片段封成 Episode，清空 segment，**重置承接摘要**（由後續壓縮階段重建）。

    呼叫端必須持 `thread.lock`；`EpisodeArchive` 會負責這件事。
    """
    if not thread.segment:
        return None
    started = thread.segment[0].at
    episode = Episode(
        id=new_episode_id(thread.entity, thread.episode_index + 1, thread.id, started),
        key=thread.key,
        thread_id=thread.id,
        index=thread.episode_index + 1,
        reason=reason,
        started_at=started,
        ended_at=float(now),
        turns=tuple(thread.segment),
    )
    thread.episode_index += 1
    thread.segment = []
    thread.carry_summary = ""
    return episode
