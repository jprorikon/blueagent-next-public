#!/usr/bin/env python3
"""回填 `stream`／`topic`（所有者 2026-11-03：記憶分流第一步，**不改行為**）。

為什麼：`stream` 是記憶的來源軸（`dialogue`＝他跟她真的說過的話／`life`＝她自己的事）。
新寫入的已經在 `add()` 裡按 source 推導；這支只負責**既有 42k 條**。

- `stream`：由 `source` 推導（`memory/facts.stream_for_source`），推不出來的算 `dialogue`。
- `topic`：只有 `life` 流填。來源是活動標題——實測 95% 的生活事實正文裡就帶著
  `「活動標題」`（`ミラ「数日前に…」3/4`）；能對上 preset 標題的就換成**穩定 key**
  （`notes-reread` 這種，改標題也不動），對不上就留標題本身（截 40 字）。

    python3 agent/tools/backfill_streams.py              # dry-run：印構成比與前幾個主題
    python3 agent/tools/backfill_streams.py --apply
"""

from __future__ import annotations

import argparse
import collections
import difflib
import os
import pathlib
import re
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

ROOT = pathlib.Path(__file__).resolve().parents[2]
ENTITIES = ("mira", "nori", "aster")
_TITLE = re.compile(r"「([^」]{2,40})」")


def _preset_keys() -> dict[str, str]:
    """`活動標題 → preset key`（每個角色一套，key 是穩定標識）。"""
    from agent.brain.runtime.life import PRESETS
    table: dict[str, str] = {}
    for presets in PRESETS.values():
        for preset in presets:
            table[str(preset.title)] = str(preset.key)
    return table


def topic_of(text: str, titles: dict[str, str]) -> str:
    """生活事實 → 主題（preset key 優先，其次原文標題）。沒有標題就回空。

    舊活動的標題措辭改過（「自分で調べて、TRPG のことを少し学ぶ」vs 現在的
    「ルールブックを…」）⇒ 完全一致だけだと**同一個主題會被拆成兩三個 topic**。
    所以再加一步相近匹配（difflib，cutoff 0.75）把措辭變體併回同一個 key。
    """
    match = _TITLE.search(str(text or ""))
    if not match:
        return ""
    title = match.group(1).strip()
    if title in titles:
        return titles[title]
    close = difflib.get_close_matches(title, list(titles), n=1, cutoff=0.75)
    if close:
        return titles[close[0]]
    return title[:40]


def _connect(data: pathlib.Path, *, readonly: bool) -> sqlite3.Connection | None:
    path = data / "memory" / "facts.db"
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path}" + ("?mode=ro" if readonly else ""),
                         uri=True, timeout=20.0)
    db.row_factory = sqlite3.Row
    return db


def _ensure_columns(db: sqlite3.Connection) -> None:
    """跟 `SqliteMemory._migrate_temporal` 同一件事（工具自己也要能跑在舊庫上）。"""
    have = {row["name"] for row in db.execute("PRAGMA table_info(facts)")}
    for name in ("stream", "topic"):
        if name not in have:
            db.execute(f"ALTER TABLE facts ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    db.commit()


def plan(data: pathlib.Path, entities=ENTITIES) -> dict:
    from agent.brain.memory.facts import stream_for_source
    titles = _preset_keys()
    db = _connect(data, readonly=True)
    if db is None:
        return {"rows": [], "stats": {}, "topics": {}}
    rows: list[dict] = []
    stats: dict = {}
    topics: dict = {}
    try:
        # 舊庫還沒有 stream/topic 欄位（新程式啟動時才會補）⇒ 這裡按實際欄位組 SQL
        have = {row["name"] for row in db.execute("PRAGMA table_info(facts)")}
        extra = ("stream" if "stream" in have else "") + (", topic" if "topic" in have else "")
        query = "SELECT id, source, text" + (", " + extra if extra else "") + " FROM facts"
        for entity in entities:
            counts = collections.Counter()
            topic_counts = collections.Counter()
            for row in db.execute(query + " WHERE entity = ?", (entity,)):
                stream = stream_for_source(str(row["source"] or ""))
                topic = topic_of(str(row["text"] or ""), titles) if stream == "life" else ""
                now_stream = str(row["stream"] or "") if "stream" in row.keys() else ""
                now_topic = str(row["topic"] or "") if "topic" in row.keys() else ""
                if now_stream != stream or now_topic != topic:
                    rows.append({"id": int(row["id"]), "entity": entity,
                                 "stream": stream, "topic": topic})
                counts[stream] += 1
                if topic:
                    topic_counts[topic] += 1
            stats[entity] = dict(counts)
            topics[entity] = topic_counts.most_common(6)
    finally:
        db.close()
    return {"rows": rows, "stats": stats, "topics": topics}


def apply(data: pathlib.Path, rows: list[dict]) -> int:
    db = _connect(data, readonly=False)
    if db is None:
        return 0
    changed = 0
    try:
        _ensure_columns(db)
        with db:
            for row in rows:
                db.execute("UPDATE facts SET stream = ?, topic = ? WHERE id = ?",
                           (row["stream"], row["topic"], int(row["id"])))
                changed += 1
        db.execute("CREATE INDEX IF NOT EXISTS facts_stream ON facts(entity, stream, created_at)")
        db.commit()
    finally:
        db.close()
    return changed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.environ.get("BLUEAGENT_DATA_DIR") or str(ROOT / "data"))
    ap.add_argument("--entity", action="append", choices=list(ENTITIES))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)

    data = pathlib.Path(a.data)
    entities = tuple(a.entity) if a.entity else ENTITIES
    result = plan(data, entities)
    if not result["stats"]:
        print(f"沒有 facts.db（{data}/memory/facts.db）⇒ 什麼都不做")
        return 1
    print(f"回填 stream/topic：待改 {len(result['rows'])} 條"
          f"　mode={'apply' if a.apply else 'dry-run'}")
    for entity, counts in result["stats"].items():
        print(f"  {entity:8s} dialogue {counts.get('dialogue', 0):6d} ／ life {counts.get('life', 0):6d}")
        for topic, count in result["topics"].get(entity, []):
            print(f"      主題 {count:5d}  {topic}")
    if not a.apply:
        print("（dry-run：什麼都沒改。要真的回填請加 --apply）")
        return 0
    print(f"回填完成：{apply(data, result['rows'])} 條")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
