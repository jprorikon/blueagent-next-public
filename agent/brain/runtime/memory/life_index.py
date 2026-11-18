"""生活流的主題索引（owner 2026-11-03；2026-11-03 從 `memory_search` 拆出來壓行數）。

為什麼要這個：生活流有 4 萬條碎片，逐條檢索只會撈到零散句子；按 `topic`（preset key）
聚合成一條線之後，「我最近在弄什麼」才答得成一條線。
"""

from __future__ import annotations

import sqlite3


def life_topics(db_path, entity: str, *, limit: int = 8) -> list[dict]:
    """主題／件數／最近一次／最近那一句（只算現行、未被取代的）。"""
    if not db_path:
        return []
    try:
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT topic, count(*) AS n, max(created_at) AS last_at FROM facts "
            "WHERE entity = ? AND stream = 'life' AND topic != '' AND superseded_by = '' "
            "GROUP BY topic ORDER BY last_at DESC LIMIT ?",
            (str(entity), max(1, int(limit)))).fetchall()
        out = []
        for row in rows:
            latest = db.execute(
                "SELECT text FROM facts WHERE entity = ? AND topic = ? AND stream = 'life' "
                "AND superseded_by = '' ORDER BY created_at DESC LIMIT 1",
                (str(entity), str(row["topic"]))).fetchone()
            out.append({"topic": str(row["topic"]), "count": int(row["n"]),
                        "last_at": float(row["last_at"] or 0),
                        "latest": str(latest["text"])[:160] if latest else ""})
        db.close()
        return out
    except sqlite3.Error:
        return []


__all__ = ["life_topics"]
