"""立场的应用层：读、写、按 principal 渲染成 prompt 里的一小段。

写入口只有一个 `set()`；`ensure_relation()` 只在第一次见到某人时播种，
不会在重启时覆盖 owner 后来调过的值。模型永远不能直接写这里。
"""

from __future__ import annotations

import time
from typing import Mapping

from agent.brain.domain.ids import norm
from agent.brain.domain.stance import (Stance, StanceAxis, StanceCatalog,
                                          strength_label)
from agent.brain.runtime.stance_store import StanceStore


class StanceService:
    def __init__(self, store: StanceStore, catalog: StanceCatalog, *,
                 top_k: int = 2, max_chars: int = 120) -> None:
        self.store = store
        self.catalog = catalog
        self.top_k = max(1, int(top_k))
        self.max_chars = max(48, int(max_chars))

    # ── 讀 ──────────────────────────────────────────────────────────────────────
    def get(self, entity: str, principal: str) -> Stance | None:
        if not str(principal or "").strip():
            return None
        return self.store.get(entity, principal)

    def snapshot(self, entity: str, names: Mapping[str, str] | None = None) -> list[dict]:
        return [self.describe(entity, stance.principal, names=names)
                for stance in self.store.snapshot(entity)]

    # ── 寫：唯一入口 ────────────────────────────────────────────────────────────
    def set(self, entity: str, principal: str, *, weights: Mapping | None = None,
            notes: Mapping | None = None, trust=None) -> Stance:
        if not str(principal or "").strip():
            raise ValueError("principal required")
        self._validate(entity, weights, notes)
        current = self.get(entity, principal)
        if current is None:
            current = Stance(entity=entity, principal=principal, trust=0.5,
                             updated_at=time.time(), revision=0)
        updated = current.changed(weights=weights, notes=notes, trust=trust)
        if updated.revision <= 0:
            updated = Stance(entity=updated.entity, principal=updated.principal,
                             axes=updated.axes, trust=updated.trust,
                             updated_at=updated.updated_at, revision=1)
        return self.store.put(updated)

    def seed(self, entity: str, principal: str, *, weights: Mapping | None = None,
             notes: Mapping | None = None, trust: float = 0.5) -> Stance:
        """显式播种一批关系轴；已有立场则原样返回（不覆盖 owner 的调整）。"""
        current = self.get(entity, principal)
        if current is not None:
            return current
        self._validate(entity, weights, notes)
        return self._ensure(entity, principal, weights=weights, notes=notes, trust=trust)

    def ensure_relation(self, entity: str, principal: str, kind: str) -> Stance:
        """第一次见到某人时播种；已有立场则原样返回（不覆盖 owner 的调整）。"""
        current = self.get(entity, principal)
        if current is not None:
            return current
        definition = self.catalog.definition(entity, kind)
        weights = {definition.id: definition.initial} if definition is not None else None
        return self._ensure(entity, principal, weights=weights)

    def _ensure(self, entity: str, principal: str, *, weights=None,
                notes=None, trust: float = 0.5) -> Stance:
        current = self.get(entity, principal)
        if current is not None:
            return current
        rows = weights if weights is not None else self._default_weights(entity)
        notes = notes if isinstance(notes, Mapping) else {}
        stance = Stance(entity=entity, principal=principal,
                        axes=tuple(StanceAxis(key, value, notes.get(key, ""))
                                   for key, value in rows.items()),
                        trust=trust, updated_at=time.time(), revision=1)
        return self.store.put(stance)

    def forget(self, entity: str, principal: str) -> bool:
        return self.store.forget(entity, principal)

    # ── 渲染 ────────────────────────────────────────────────────────────────────
    def render(self, entity: str, principal: str, *, addressed: str = "",
               names: Mapping[str, str] | None = None,
               max_chars: int | None = None) -> str:
        """把说话者（以及被称呼者，如果不同）的立场压成 ≤max_chars 的块。"""
        speaker = norm(principal, fallback="")
        if not speaker:
            return ""
        targets = [speaker]
        other = norm(addressed, fallback="")
        if other and other not in {"entity", speaker}:
            targets.append(other)
        limit = self.max_chars if max_chars is None else max(48, int(max_chars))
        per_target = limit if len(targets) == 1 else max(44, (limit - 8) // len(targets))
        names = names or {}
        lines = []
        for target in targets:
            stance = self.get(entity, target)
            if stance is None:
                continue
            line = self._line(stance, target, names,
                              limit=self.top_k if len(targets) == 1 else 1,
                              budget=per_target)
            if line:
                lines.append(line)
        if not lines:
            return ""
        body = "\n".join(lines)
        if len(body) > limit:
            body = self._fit(body, limit)
        return f"[[stance]]\n{body}\n[[/stance]]"

    def describe(self, entity: str, principal: str,
                 names: Mapping[str, str] | None = None) -> dict:
        stance = self.get(entity, principal)
        if stance is None:
            return {"entity": norm(entity, fallback=""), "principal": principal,
                    "trust": 0.0, "axes": [], "rendered": "", "revision": 0}
        rows = []
        for axis in stance.axes:
            definition = self.catalog.definition(entity, axis.id)
            rows.append({
                "id": axis.id,
                "label": definition.label if definition is not None else axis.id,
                "weight": axis.weight,
                "tier": strength_label(axis.weight),
                "note": axis.note,
                "tone": definition.tone if definition is not None else "",
            })
        return {"entity": stance.entity, "principal": stance.principal,
                "trust": round(stance.trust, 2), "axes": rows,
                "updated_at": stance.updated_at, "revision": stance.revision,
                "rendered": self.render(entity, principal, names=names)}

    # ── 內部 ────────────────────────────────────────────────────────────────────
    def _default_weights(self, entity: str) -> dict[str, float]:
        guest = self.catalog.definition(entity, "guest")
        if guest is not None:
            return {guest.id: guest.initial}
        return {}

    def _validate(self, entity: str, weights: Mapping | None,
                  notes: Mapping | None) -> None:
        for rel_id in (weights or {}):
            if not self.catalog.has(entity, str(rel_id)):
                raise ValueError(f"unknown relationship for {entity}: {rel_id}")
        for rel_id in (notes or {}):
            if not self.catalog.has(entity, str(rel_id)):
                raise ValueError(f"unknown relationship for {entity}: {rel_id}")

    def _line(self, stance: Stance, target: str, names: Mapping[str, str],
              *, limit: int, budget: int) -> str:
        axes = stance.top(limit)
        parts = []
        for index, axis in enumerate(axes):
            definition = self.catalog.definition(stance.entity, axis.id)
            if definition is None:
                continue
            head = "主" if index == 0 and len(axes) > 1 else (
                "副" if index > 0 else "")
            text = (axis.note or definition.tone).rstrip("。．.!！?？;； ")
            part = f"{head}「{definition.label}」（{axis.tier}）"
            if text:
                part += f"——{text}"
            parts.append(part)
        if not parts:
            return ""
        name = str(names.get(target) or target)
        return self._fit(f"{name} に対して：" + "／".join(parts), budget)

    @staticmethod
    def _fit(text: str, budget: int) -> str:
        text = " ".join(str(text or "").split()).strip()
        if len(text) <= budget:
            return text
        cut = text[:max(1, budget - 1)]
        for sep in ("／", "。", "、", "，"):
            index = cut.rfind(sep)
            if index >= budget // 2:
                cut = cut[:index]
                break
        return cut.rstrip("，,；;。.、 ") + "…"


__all__ = ["StanceService"]
