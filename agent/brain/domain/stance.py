"""立场（stance）：**角色对某个人**的慢变量，不是人跟人的关系图。

这条边界很重要：

  · `SocialGraph` 的边是 A–B：谁认识谁、记忆允许谁知道（audience）。
  · `Stance` 的键是 `(entity, principal)`：**她怎么看这个人**，只影响语气、称呼、距离感。
  · 两者可以指向同一个人，但绝不互相授权。立场再高也不能打开 pair/private 记忆。

一个 principal 同时可以有多条关系轴（主人、恋人、朋友……），各自有权重；
`tone` / `label` 属于角色定义，`note` 是 owner 手写的就地覆盖。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, Mapping

from agent.brain.domain.ids import norm

#: 只给强度档，不把数字塞进 prompt（模型对档位比数字敏感）。
TIERS = ((0, 14, "ごく弱"), (15, 34, "弱"), (35, 54, "中"),
         (55, 79, "強"), (80, 100, "とても強"))


def strength_label(weight: float) -> str:
    value = max(0.0, min(100.0, float(weight)))
    for low, high, label in TIERS:
        if low <= value <= high:
            return label
    return "とても強" if value > 100 else "ごく弱"


def _weight(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(100.0, number)), 3)


@dataclass(frozen=True, slots=True)
class StanceDefinition:
    """一个角色自己的关系轴定义；语义（tone）必须用这个角色的口吻写。"""

    id: str
    label: str
    tone: str = ""
    initial: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", norm(self.id, fallback="relation"))
        object.__setattr__(self, "label", str(self.label or self.id).strip())
        object.__setattr__(self, "tone", " ".join(str(self.tone or "").split()))
        object.__setattr__(self, "initial", _weight(self.initial))

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "tone": self.tone,
                "initial": self.initial}


@dataclass(frozen=True, slots=True)
class StanceAxis:
    """一条关系轴的当前值；`note` 覆盖定义里的 tone。"""

    id: str
    weight: float = 0.0
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", norm(self.id, fallback="relation"))
        object.__setattr__(self, "weight", _weight(self.weight))
        object.__setattr__(self, "note", " ".join(str(self.note or "").split())[:240])

    @property
    def tier(self) -> str:
        return strength_label(self.weight)

    def to_dict(self) -> dict:
        return {"id": self.id, "weight": self.weight, "note": self.note}


@dataclass(frozen=True, slots=True)
class Stance:
    entity: str
    principal: str
    axes: tuple[StanceAxis, ...] = ()
    trust: float = 0.5
    updated_at: float = 0.0
    revision: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "entity", norm(self.entity, fallback="default"))
        object.__setattr__(self, "principal", norm(self.principal, fallback="owner"))
        table: dict[str, StanceAxis] = {}
        for axis in self.axes:
            table[axis.id] = axis
        ordered = tuple(sorted(table.values(), key=lambda axis: (-axis.weight, axis.id)))
        object.__setattr__(self, "axes", ordered)
        object.__setattr__(self, "trust", max(0.0, min(1.0, float(self.trust or 0.0))))
        object.__setattr__(self, "updated_at", float(self.updated_at or 0.0))
        object.__setattr__(self, "revision", max(0, int(self.revision or 0)))

    def axis(self, rel_id: str) -> StanceAxis | None:
        key = norm(rel_id, fallback="")
        for axis in self.axes:
            if axis.id == key:
                return axis
        return None

    def weight(self, rel_id: str) -> float:
        axis = self.axis(rel_id)
        return axis.weight if axis is not None else 0.0

    def top(self, limit: int = 2) -> tuple[StanceAxis, ...]:
        rows = [axis for axis in self.axes if axis.weight > 0]
        return tuple(rows[:max(0, int(limit))])

    def changed(self, *, weights: Mapping[str, object] | None = None,
                notes: Mapping[str, object] | None = None, trust=None,
                now: float | None = None) -> "Stance":
        table = {axis.id: axis for axis in self.axes}
        for rel_id, value in (weights or {}).items():
            key = norm(rel_id, fallback="")
            old = table.get(key)
            table[key] = StanceAxis(key, value, old.note if old is not None else "")
        for rel_id, value in (notes or {}).items():
            key = norm(rel_id, fallback="")
            old = table.get(key)
            table[key] = StanceAxis(key, old.weight if old is not None else 0.0, value)
        return Stance(
            entity=self.entity, principal=self.principal, axes=tuple(table.values()),
            trust=self.trust if trust is None else trust,
            updated_at=time.time() if now is None else float(now),
            revision=self.revision + 1,
        )

    def to_dict(self) -> dict:
        return {
            "entity": self.entity, "principal": self.principal,
            "axes": [axis.to_dict() for axis in self.axes],
            "trust": round(self.trust, 3), "updated_at": self.updated_at,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, raw: Mapping | None) -> "Stance | None":
        if not isinstance(raw, Mapping):
            return None
        axes = raw.get("axes")
        if isinstance(axes, list):
            parsed = []
            for row in axes:
                if isinstance(row, Mapping):
                    parsed.append(StanceAxis(str(row.get("id") or ""),
                                             row.get("weight", 0),
                                             str(row.get("note") or "")))
        else:
            weights = raw.get("weights")
            notes = raw.get("notes")
            parsed = []
            if isinstance(weights, Mapping):
                notes = notes if isinstance(notes, Mapping) else {}
                for rel_id, value in weights.items():
                    parsed.append(StanceAxis(str(rel_id), value,
                                             str(notes.get(rel_id) or "")))
        return cls(
            entity=str(raw.get("entity") or "default"),
            principal=str(raw.get("principal") or "owner"),
            axes=tuple(parsed), trust=raw.get("trust", 0.5),
            updated_at=raw.get("updated_at", 0.0),
            revision=raw.get("revision", 0),
        )


class StanceCatalog:
    """角色绑定的关系定义库；机制共用，语义不共用。"""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, StanceDefinition]] = {}

    def add(self, entity: str, definitions: Iterable[StanceDefinition]) -> None:
        table: dict[str, StanceDefinition] = {}
        for definition in definitions:
            table[definition.id] = definition
        self._items[norm(entity, fallback="default")] = table

    def definitions(self, entity: str) -> tuple[StanceDefinition, ...]:
        table = self._items.get(norm(entity, fallback=""), {})
        return tuple(table[key] for key in sorted(table))

    def definition(self, entity: str, rel_id: str) -> StanceDefinition | None:
        return self._items.get(norm(entity, fallback=""), {}).get(
            norm(rel_id, fallback=""))

    def has(self, entity: str, rel_id: str) -> bool:
        return self.definition(entity, rel_id) is not None

    def entities(self) -> tuple[str, ...]:
        return tuple(sorted(self._items))


__all__ = ["Stance", "StanceAxis", "StanceCatalog", "StanceDefinition",
           "strength_label", "TIERS"]
