"""`fact_audience` 索引：把 `facts.audience`（逗號串接 TEXT）與 `facts.principal` 投影成可查的表。

為什麼要有這張表（owner 2026-11-16「拆 fact_audience 表让焦点窗快速校准」）：
注意力場的焦點窗要問「這條事實跟這個人有關嗎」，原本只能 `audience LIKE '%owner%'`
掃全表（4 萬行、一直在長）。拆表後是一次 `(member, entity, created_at)` 索引查詢。

三條規則：
1. **索引只是投影，權威永遠是 `facts` 那一行**：`audience IS NULL`（世界事實）就不進這張表，
   查詢端要另外處理「世界事實走底池」。
2. **寫入路徑同步**：`SqliteMemory.add()` 在同一個 transaction 之後呼叫 `index_fact()`；
   存量由 `agent/tools/migrations/backfill_fact_audience_20261116.py` 補（INSERT OR IGNORE，可重跑）。
3. **取代／過期不用動這張表**：查詢端一定 JOIN `facts` 並過 `superseded_by`／`valid_to`，
   所以舊值的受眾列留著不會讓它復活。
4. **刪除要跟著刪**（觸發器 `fact_audience_ad`）：`facts.id` 會被重用，舊列留著就會掛錯人。
5. **「完整」要有明確標記**（審核 R2，2026-11-17）：以前查詢端看「表裡有沒有任何一行」就切到索引——
   升級後、回填前只要寫進一條新事實，未回填的舊事實就從焦點窗消失。現在 `fact_audience_meta` 記
   `backfill=<版本>`：回填開始先清、整張 `facts` 掃完才置位；查詢端只認這個標記（`is_complete`）。
   開庫時 `ensure_ready()`：已標記 ⇒ 不動；沒有存量事實 ⇒ 直接標記；有存量 ⇒ 當場補跑一次（可重跑）。
"""

from __future__ import annotations

import sqlite3

TABLE = "fact_audience"
META = "fact_audience_meta"
#: 回填版本：投影規則改了就加一，舊標記自動失效（查詢端退回 LIKE，開庫時重跑）
BACKFILL_VERSION = "1"


def ensure(db: sqlite3.Connection) -> None:
    """建表＋索引（舊庫第一次跑到時補上；schema 那份也會建，這裡是保險）。"""
    db.executescript(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("
        " fact_id INTEGER NOT NULL, member TEXT NOT NULL,"
        " kind TEXT NOT NULL DEFAULT 'audience', entity TEXT NOT NULL DEFAULT '',"
        " created_at REAL NOT NULL DEFAULT 0,"
        " PRIMARY KEY (fact_id, kind, member));"
        f"CREATE INDEX IF NOT EXISTS {TABLE}_member"
        f" ON {TABLE}(member, entity, created_at DESC);"
        # facts 刪掉 ⇒ 投影跟著刪（id 會被重用，舊列不能留）；schema 那份也有，這裡是保險
        f"CREATE TRIGGER IF NOT EXISTS {TABLE}_ad AFTER DELETE ON facts BEGIN"
        f" DELETE FROM {TABLE} WHERE fact_id = old.id; END;"
        f"CREATE TABLE IF NOT EXISTS {META} (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    db.commit()


def is_complete(db: sqlite3.Connection) -> bool:
    """回填是否**完整**做完（唯讀連線也能問；表不在／讀不到一律當沒做完）。"""
    try:
        row = db.execute(f"SELECT value FROM {META} WHERE key='backfill'").fetchone()
    except sqlite3.Error:
        return False
    return row is not None and str(row[0]) == BACKFILL_VERSION


def _set_complete(db: sqlite3.Connection, done: bool) -> None:
    if done:
        db.execute(f"INSERT OR REPLACE INTO {META} (key, value) VALUES ('backfill', ?)", (BACKFILL_VERSION,))
    else:
        db.execute(f"DELETE FROM {META} WHERE key='backfill'")
    db.commit()


def ensure_ready(db: sqlite3.Connection) -> str:
    """開庫時呼叫：確保「完整」標記可信。回 `ready`／`fresh`／`backfilled`／`failed`。"""
    ensure(db)
    if is_complete(db):
        return "ready"
    if db.execute("SELECT 1 FROM facts LIMIT 1").fetchone() is None:
        _set_complete(db, True)              # 空庫：之後每條都由寫入路徑同步，天生完整
        return "fresh"
    try:
        backfill(db)
    except sqlite3.Error:
        return "failed"                      # 標記沒置位 ⇒ 查詢端繼續走 LIKE，結果仍正確
    return "backfilled" if is_complete(db) else "failed"


def has_table(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                     (TABLE,)).fetchone()
    return row is not None


def index_fact(db: sqlite3.Connection, fact_id: int, fact) -> int:
    """把一條事實的受眾／principal 寫進索引（回寫了幾列）。`audience=None` 只記 principal。"""
    if not fact_id:
        return 0
    rows = []
    audience = getattr(fact, "audience", None)
    if audience:
        rows += [(int(fact_id), str(member), "audience",
                  str(getattr(fact, "entity", "") or ""),
                  float(getattr(fact, "created_at", 0) or 0)) for member in sorted(audience)]
    principal = str(getattr(fact, "principal", "") or "")
    if principal:
        rows.append((int(fact_id), principal, "principal",
                     str(getattr(fact, "entity", "") or ""),
                     float(getattr(fact, "created_at", 0) or 0)))
    if not rows:
        return 0
    db.executemany(
        f"INSERT OR REPLACE INTO {TABLE} (fact_id, member, kind, entity, created_at)"
        " VALUES (?,?,?,?,?)", rows)
    return len(rows)


def backfill(db: sqlite3.Connection, *, batch: int = 5000,
             limit_rows: int | None = None) -> dict:
    """存量回填：把 `facts.audience`／`principal` 投影進索引。**可重跑**（INSERT OR IGNORE 語意）。

    先清「完整」標記（回填期間查詢端走 LIKE），整張 `facts` 掃完才置位；`limit_rows` 中途停下不置位。
    """
    ensure(db)
    _set_complete(db, False)
    finished = False
    written = 0
    scanned = 0
    last_id = 0
    while True:
        sql = ("SELECT id, audience, principal, entity, created_at FROM facts "
               "WHERE id > ? ORDER BY id LIMIT ?")
        rows = db.execute(sql, (last_id, max(1, int(batch)))).fetchall()
        if not rows:
            finished = True
            break
        payload = []
        for row in rows:
            fact_id, audience, principal, entity, created_at = row
            for member in [item.strip() for item in str(audience or "").split(",") if item.strip()]:
                payload.append((int(fact_id), member, "audience", str(entity or ""),
                                float(created_at or 0)))
            if str(principal or "").strip():
                payload.append((int(fact_id), str(principal).strip(), "principal",
                                str(entity or ""), float(created_at or 0)))
        if payload:
            db.executemany(
                f"INSERT OR IGNORE INTO {TABLE} (fact_id, member, kind, entity, created_at)"
                " VALUES (?,?,?,?,?)", payload)
            db.commit()
            written += len(payload)
        scanned += len(rows)
        last_id = int(rows[-1][0])
        if limit_rows is not None and scanned >= int(limit_rows):
            break
    if finished:
        _set_complete(db, True)
    return {"scanned": scanned, "written": written, "rows": count(db), "complete": finished}


def count(db: sqlite3.Connection) -> int:
    try:
        return int(db.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])
    except sqlite3.Error:
        return 0


__all__ = ["TABLE", "META", "BACKFILL_VERSION", "backfill", "count", "ensure", "ensure_ready",
           "has_table", "index_fact", "is_complete"]
