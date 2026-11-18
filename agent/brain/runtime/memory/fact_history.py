"""記憶入檔履歷（`facts_history`）的唯一讀寫入口＋「記憶庫在哪個檔」的唯一判法。

review.md（2026-10-25）P1：`GET /api/memory/trace` 自己從 memory 物件猜 SQLite 路徑、
直接下 SQL；提煉器又各自猜一次路徑、各自寫 SQL。現在：
  · `memory_db_path(memory)`：三種後端的庫檔位置（記憶體版沒有 ⇒ 空字串）；
  · `FactHistory.append(rows)`：提煉器每次判定寫一筆（會先確保表存在）；
  · `FactHistory.for_fact(fact_key)`：查一條記憶的來歷；表還不存在就回空、不建表。

⚠ 以前提煉器的寫法是 `getattr(getattr(memory, "path", None), "__str__", …)()`：記憶體版
沒有 `path` ⇒ 得到字串 `"None"` ⇒ 在工作目錄開一個叫 `None` 的 SQLite 檔。這裡回空字串。
"""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts_history (
    id         INTEGER PRIMARY KEY,
    fact_key   TEXT NOT NULL,           -- 正規化後的事實文字（跨倉唯一鍵）
    entity     TEXT NOT NULL DEFAULT '',
    action     TEXT NOT NULL,           -- insert | skip | update
    confidence REAL NOT NULL DEFAULT 0,
    reason     TEXT NOT NULL DEFAULT '',
    evidence   TEXT NOT NULL DEFAULT '',-- thread:turn 逗號分隔
    run_id     TEXT NOT NULL DEFAULT '',
    model      TEXT NOT NULL DEFAULT '',
    at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS facts_history_key ON facts_history(fact_key, at);
"""


def memory_db_path(memory) -> str:
    """記憶後端的 SQLite 檔：SqliteMemory 是 `.path`，HindsightMemory 是 `.local.path`；
    記憶體版（InMemoryMemory）沒有庫檔 ⇒ 空字串。"""
    for holder in (getattr(memory, "local", None), memory):
        path = getattr(holder, "path", None)
        if path:
            return str(path)
    return ""


class FactHistory:
    def __init__(self, path) -> None:
        self.path = str(path or "")

    def append(self, rows: list[dict], *, model: str = "") -> None:
        """寫判定履歷（insert／skip／update 都記）。沒有庫檔就不寫。"""
        if not rows or not self.path:
            return
        db = sqlite3.connect(self.path)
        try:
            db.executescript(SCHEMA)
            with db:
                db.executemany(
                    "INSERT INTO facts_history (fact_key, entity, action, confidence, reason,"
                    " evidence, run_id, model, at) VALUES (?,?,?,?,?,?,?,?,?)",
                    [(row["fact_key"], row["entity"], row["action"], row["confidence"],
                      row["reason"], ",".join(row["evidence"]), row["run_id"], model,
                      row["at"]) for row in rows])
        finally:
            db.close()

    def for_fact(self, fact_key: str, *, limit: int = 20) -> list[dict]:
        """這條記憶的來歷（新的在前）。表還不存在（還沒提煉過）⇒ 空串列。"""
        if not self.path:
            return []
        try:
            db = sqlite3.connect(self.path)
            db.row_factory = sqlite3.Row
            try:
                return [dict(row) for row in db.execute(
                    "SELECT * FROM facts_history WHERE fact_key = ? ORDER BY at DESC LIMIT ?",
                    (str(fact_key), max(1, min(200, int(limit))))).fetchall()]
            finally:
                db.close()
        except sqlite3.Error:
            return []


__all__ = ["FactHistory", "SCHEMA", "memory_db_path"]
