"""事实的 JSONL 持久化：一行一条，只追加，不改写。

这是「对话原文」之外的第一层长期记忆。原文还在 episode/thread 里；
事实是检索用的投影，坏行跳过，不让一条脏数据拖垮启动。

两种行（2026-11-15 返工 R1）：

  · 事实行：`Fact.to_store_dict()`，带 `text`。
  · 更新记录：`{"op": "retire_slot"|"supersede"|"revive", ...}`，**没有** `text`。
    取代／槽位退场／复活会改既有行的指针与 `valid_to`；这些改动以前要么丢（重建后回到取代前），
    要么靠「删档再整份重写」补（会丢掉只在正本里的行、非原子、有并发窗口）。
    现在改成**追加一条记录**，重建时按文件顺序重放——正本仍然只增不改。

旧版读到更新记录：没有 `text` ⇒ `load()` 本来就跳过，不会报错。
"""

from __future__ import annotations

import json
import os
import pathlib
import threading

from agent.brain.memory.facts import Fact

#: 认得的更新记录种类（重放端只认这几种，其余跳过）
OPS = frozenset({"retire_slot", "supersede", "revive"})


class FactStore:
    def __init__(self, path) -> None:
        self.path = pathlib.Path(path)
        self._lock = threading.RLock()

    def _rows(self) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            return []
        rows: list[dict] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(raw, dict):
                rows.append(raw)
        return rows

    def load(self) -> list[Fact]:
        """只回事实行（更新记录与坏行跳过）。"""
        return [fact for fact in self.load_records() if isinstance(fact, Fact)]

    def load_records(self) -> list:
        """按文件顺序回 `Fact` 或更新记录（`dict`，带 `op`）。不认识的行跳过。"""
        out: list = []
        for raw in self._rows():
            if "op" in raw:
                if str(raw.get("op") or "") in OPS:
                    out.append(dict(raw))
                continue
            try:
                fact = Fact.from_store_dict(raw)
            except (ValueError, TypeError):
                continue
            if fact.text:
                out.append(fact)
        return out

    def append(self, fact: Fact) -> None:
        if not fact.text:
            return
        self._write(fact.to_store_dict())

    def append_op(self, op: str, **fields) -> None:
        """追加一条更新记录（取代／槽位退场／复活）。不改写任何既有行。"""
        if op not in OPS:
            raise ValueError(f"unknown fact-store op: {op!r}")
        self._write({"op": op, **fields})

    def _write(self, row: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
        except OSError:
            pass

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


__all__ = ["FactStore", "OPS"]
