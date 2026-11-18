"""獨白／自主任務要用的兩塊「關於她自己」的事實（2026-11-16）。

為什麼獨立成一支：`heartbeat.py` 已頂到 600 行棘輪，而這兩件事都只是**讀檔＋小快取**。

1. `locked_items(base, entity)`：台帳裡 `state=locked` 的事（「在等誰回話」「等某個時間」）。
   提案⑥ 的 `waiting` 要跟它雙向一致——台帳裡沒有這條，模型說的「我在等他回」就是編的。
2. `self_block(base, entity)`：自傳式記憶（提案⑤）的那一段。**每小時最多讀一次檔**，
   因為心跳 30–150 秒一跳，每跳都讀檔會白白打 IO。
"""

from __future__ import annotations

import pathlib
import time

#: 自傳式記憶的快取壽命（秒）：重建一天一次，這裡只要不每跳讀檔
SELF_BLOCK_TTL_S = 3600.0
_LEDGERS: dict[tuple[str, str], object] = {}
_SELF_CACHE: dict[tuple[str, str], tuple[float, str]] = {}


def ledger_for(base, entity: str):
    """每個角色一份 TaskLedger（**快取**：建構會讀檔，每跳重建會拖慢心跳）。"""
    key = (str(base or ""), str(entity))
    ledger = _LEDGERS.get(key)
    if ledger is None:
        from agent.brain.runtime.inner.task_ledger import TaskLedger
        ledger = TaskLedger(pathlib.Path(base) / "tasks", str(entity))
        _LEDGERS[key] = ledger
    return ledger


def locked_items(base, entity: str, *, limit: int = 3) -> list[str]:
    """台帳裡「被條件卡住」的幾條（給獨白的 `waiting` 當依據）。"""
    if not base:
        return []
    try:
        rows = ledger_for(base, entity).items()
    except Exception:                                      # noqa: BLE001 - 台帳壞了當沒有
        return []
    out = []
    for row in rows:
        if str(row.get("state") or "") != "locked":
            continue
        title = str(row.get("title") or "")[:24]
        note = str(row.get("note") or "")[:16]
        out.append(f"{title}（{note}）" if note else title)
    return out[:limit]


def self_block(base, entity: str, *, now: float | None = None,
               ttl_s: float = SELF_BLOCK_TTL_S) -> str:
    """自傳式記憶那一段（沒有檔案／還沒重建就回空字串）。"""
    if not base:
        return ""
    key = (str(base), str(entity))
    moment = float(now if now is not None else time.time())
    cached = _SELF_CACHE.get(key)
    if cached and moment - cached[0] < ttl_s:
        return cached[1]
    try:
        from agent.brain.runtime.inner import self_model
        block = self_model.prompt_block(base, entity)
    except Exception:                                      # noqa: BLE001 - 讀不到就不給
        block = ""
    _SELF_CACHE[key] = (moment, block)
    return block


def forget(entity: str | None = None) -> None:
    """測試用：清掉快取（重建之後想立刻看到新內容）。"""
    for table in (_SELF_CACHE, _LEDGERS):
        for key in list(table):
            if entity is None or key[1] == str(entity):
                table.pop(key, None)


__all__ = ["SELF_BLOCK_TTL_S", "forget", "ledger_for", "locked_items", "self_block"]
