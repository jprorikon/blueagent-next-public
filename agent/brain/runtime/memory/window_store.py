"""提煉窗口摘要（`window_summaries`）的**唯一**讀寫入口。

以前表結構定義在 `distill.py`，`memory_search`（四處）與 `server/api` 各自直接寫 SQL 讀它——
欄位一改就得翻四個地方（2026-10-25 結構 review 第 3 階段）。現在：
  · 寫：`upsert(record)`（提煉器）——會先確保表存在；
  · 讀：`recent`／`overlapping`／`items`——表還不存在（還沒提煉過）就回空，不建表、不拋例外。

披露規則**不在這裡**：這一層只管資料，「這幾個人面前說不說得出口」由 `memory_search` 判。
"""

from __future__ import annotations

import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS window_summaries (
    window_id  TEXT PRIMARY KEY,
    entity     TEXT NOT NULL,
    thread     TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL DEFAULT 0,
    ended_at   REAL NOT NULL DEFAULT 0,
    turns      INTEGER NOT NULL DEFAULT 0,
    topic      TEXT NOT NULL DEFAULT '',
    summary    TEXT NOT NULL DEFAULT '',
    people     TEXT NOT NULL DEFAULT '',
    open_loops TEXT NOT NULL DEFAULT '',
    run_id     TEXT NOT NULL DEFAULT '',
    at         REAL NOT NULL DEFAULT 0,
    game       INTEGER NOT NULL DEFAULT 0   -- 1＝TRPG 卓上（虛構）；0＝現實（2026-11-11 B3）
);
CREATE INDEX IF NOT EXISTS window_summaries_entity ON window_summaries(entity, ended_at);
"""

#: 門牌（2026-10-25）：`ptr` 是回原文的指針（`raw:<entity>#t=<turn>+<n>`），
#: `turn_ids` 是這一段的逐輪 id。**沒有門牌的摘要只算索引，不算記憶**（ECHO 規矩 1）。
#: 舊庫靠 `_migrate()` 就地補欄位——不重建表、不動既有資料。
LATE_COLUMNS = (("turn_ids", "TEXT NOT NULL DEFAULT '[]'"),
                ("ptr", "TEXT NOT NULL DEFAULT ''"),
                # 場景維度（2026-11-11 B3）：舊窗口一律留 0＝現實
                ("game", "INTEGER NOT NULL DEFAULT 0"))



def window_age(row, *, now: float | None = None) -> str:
    """窗口摘要結束到現在多久：「さっき／25分前／3時間前（23:10ごろ）／11月9日 23:10ごろ」。

    （`runtime/memory` 不准往旁邊 import `inner/timeframe`，所以這裡自己寫一支小的。）
    """
    import time as _time

    try:
        ended = float(row["ended_at"] or 0.0)
    except (KeyError, IndexError, TypeError, ValueError):
        return ""
    if not ended:
        return ""
    at = _time.time() if now is None else float(now)
    gap = max(0.0, at - ended)
    clock = _time.strftime("%H:%M", _time.localtime(ended))
    if gap < 90:
        return "さっき"
    if gap < 3600:
        return f"{int(gap // 60)}分前"
    if gap < 86400:
        return f"{int(gap // 3600)}時間前・{clock}ごろ"
    return _time.strftime("%m月%d日 ", _time.localtime(ended)) + f"{clock}ごろ"


def window_room(rooms: dict[str, list[tuple[float, frozenset]]], row) -> frozenset:
    """窗口的房間＝那段原文每一輪房間的**交集**（最嚴）。

    先取窗口時間內的輪次；時間對不上（原文已輪轉、時鐘誤差）就退回整條線的交集——
    同一條線的參與者只增不減，整條線的交集只會更嚴，不會更鬆。
    （2026-11-11 從 `memory_search` 搬來這裡：窗口的東西歸窗口，那邊也順便壓回 600 行棘輪下。）
    """
    entries = rooms.get(str(row["thread"] or ""), [])
    start, end = float(row["started_at"] or 0), float(row["ended_at"] or 0)
    inside = [room for at, room in entries if start <= at <= end] or [room for _, room in entries]
    if not inside:
        return frozenset()
    return frozenset.intersection(*inside)


class WindowStore:
    def __init__(self, path) -> None:
        self.path = str(path or "")

    def _read(self, sql: str, params: tuple) -> list[sqlite3.Row]:
        if not self.path:
            return []
        try:
            db = sqlite3.connect(self.path)
            db.row_factory = sqlite3.Row
        except sqlite3.Error:
            return []
        try:
            try:
                return db.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                # 舊庫還沒有新欄位（`turn_ids`／`ptr`／`game`）⇒ 就地補一次再讀。
                # 沒有這一步，舊庫上「只要現實窗口」那種查詢會整條回空（看起來像記憶全消失）。
                _migrate(db)
                db.commit()
                return db.execute(sql, params).fetchall()
        except sqlite3.Error:
            return []
        finally:
            db.close()

    # ── 讀 ──────────────────────────────────────────────────────────────────
    def recent(self, entity: str, limit: int = 200, *,
               game: int | None = None) -> list[sqlite3.Row]:
        """這個角色最近的窗口（新的在前）。

        `game`（2026-11-11 B4）：`0`＝只要現實的窗口、`1`＝只要桌上的；`None`＝不過濾。
        心跳／主動消息的「最近話したこと」必須傳 0，否則會把桌上那段摘要當成現況。
        """
        if game is None:
            return self._read("SELECT * FROM window_summaries WHERE entity = ?"
                              " ORDER BY ended_at DESC LIMIT ?", (str(entity), int(limit)))
        return self._read("SELECT * FROM window_summaries WHERE entity = ?"
                          " AND COALESCE(game, 0) = ?"
                          " ORDER BY ended_at DESC LIMIT ?",
                          (str(entity), int(game), int(limit)))

    def overlapping(self, entity: str, started_at: float, ended_at: float, *,
                    margin_s: float = 60.0, limit: int = 20) -> list[sqlite3.Row]:
        """跟 `[started_at, ended_at]`（前後各放寬 `margin_s`）重疊的窗口（新的在前）。"""
        return self._read("SELECT * FROM window_summaries"
                          " WHERE entity = ? AND ended_at >= ? AND started_at <= ?"
                          " ORDER BY ended_at DESC LIMIT ?",
                          (str(entity), float(started_at) - margin_s,
                           float(ended_at) + margin_s, int(limit)))

    def items(self, entity: str) -> list[tuple[str, str]]:
        """`(window_id, "topic summary")`：向量索引要嵌入的文字。"""
        return [(str(row["window_id"]), f"{row['topic']} {row['summary']}")
                for row in self._read("SELECT window_id, topic, summary FROM window_summaries"
                                      " WHERE entity = ?", (str(entity),))]

    def get(self, window_id: str) -> sqlite3.Row | None:
        rows = self._read("SELECT * FROM window_summaries WHERE window_id = ?",
                          (str(window_id),))
        return rows[0] if rows else None

    def stats(self, entity: str | None = None) -> dict:
        """體檢用：條數／帶門牌幾條／最後一條摘要的時間。

        ★ 為什麼要有它：門牌覆蓋率是**這一層的失敗模式**（沒有它，摘要就只是一份
        沒人核對得動的轉述）。體檢器要能自己算出來，不能靠「看起來有裝」。
        """
        rows = self._read("SELECT window_id, ptr, turn_ids, ended_at FROM window_summaries"
                          + ("" if entity is None else " WHERE entity = ?"),
                          () if entity is None else (str(entity),))
        if not rows:
            # 舊庫還沒有 ptr 欄位時，至少要把「有幾條」算出來——否則體檢會顯示成
            # 「索引層沒東西」，而真相是「索引層有東西，但沒有門牌」。
            rows = self._read("SELECT window_id, ended_at FROM window_summaries"
                              + ("" if entity is None else " WHERE entity = ?"),
                              () if entity is None else (str(entity),))
            if not rows:
                return {"total": 0, "with_ptr": 0, "with_turn_ids": 0, "newest_at": 0.0}
            return {"total": len(rows), "with_ptr": 0, "with_turn_ids": 0,
                    "newest_at": max((float(row["ended_at"] or 0) for row in rows),
                                     default=0.0)}
        with_ptr = sum(1 for row in rows if str(row["ptr"] or "").strip())
        with_ids = sum(1 for row in rows if str(row["turn_ids"] or "").strip() not in ("", "[]"))
        newest = max((float(row["ended_at"] or 0) for row in rows), default=0.0)
        return {"total": len(rows), "with_ptr": with_ptr, "with_turn_ids": with_ids,
                "newest_at": newest}

    # ── 寫 ──────────────────────────────────────────────────────────────────
    def upsert(self, record: dict) -> None:
        """寫一條窗口摘要（同 window_id 覆蓋）。寫壞了不拋例外：檔案版仍是正本。"""
        if not self.path:
            return
        try:
            db = sqlite3.connect(self.path)
        except sqlite3.Error:
            return
        try:
            db.executescript(SCHEMA)
            _migrate(db)
            with db:
                db.execute(
                    "INSERT OR REPLACE INTO window_summaries (window_id, entity, thread,"
                    " started_at, ended_at, turns, topic, summary, people, open_loops,"
                    " run_id, at, turn_ids, ptr, game)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (record["window_id"], record["entity"], str(record.get("thread") or ""),
                     float(record.get("started_at") or 0), float(record.get("ended_at") or 0),
                     int(record.get("turns") or 0), record.get("topic") or "",
                     record.get("summary") or "",
                     json.dumps(record.get("people") or [], ensure_ascii=False),
                     json.dumps(record.get("open_loops") or [], ensure_ascii=False),
                     record.get("run_id") or "", float(record.get("at") or 0),
                     json.dumps(record.get("turn_ids") or [], ensure_ascii=False),
                     str(record.get("ptr") or ""), int(record.get("game") or 0)))
        except sqlite3.Error:
            pass
        finally:
            db.close()


def _migrate(db: sqlite3.Connection) -> None:
    """舊庫就地補欄位（`turn_ids` / `ptr`）。缺欄位只影響門牌，不影響讀取舊摘要。"""
    try:
        have = {str(row[1]) for row in db.execute("PRAGMA table_info(window_summaries)")}
    except sqlite3.Error:
        return
    for name, decl in LATE_COLUMNS:
        if name in have:
            continue
        try:
            db.execute(f"ALTER TABLE window_summaries ADD COLUMN {name} {decl}")
        except sqlite3.Error:
            pass


__all__ = ["window_age", "LATE_COLUMNS", "SCHEMA", "WindowStore"]
