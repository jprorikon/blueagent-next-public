#!/usr/bin/env python3
"""把 `facts.audience`／`principal` 回填进 `fact_audience` 索引（owner 2026-11-16 拆表）。

預設**只讀盤點**；`--apply` 才寫。可重跑（INSERT OR IGNORE），不需要停服務——
這張表只是投影，權威仍是 `facts` 那一行；寫入路徑（`SqliteMemory.add`）已經同步維護它。

    python3 agent/tools/migrations/backfill_fact_audience_20261116.py --db data/memory/facts.db
    python3 agent/tools/migrations/backfill_fact_audience_20261116.py --db ... --apply

驗收（`--verify` 會一起做）：同一組焦點查詢，走索引與走 `audience LIKE` 的結果必須**完全一致**。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sqlite3
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]   # agent/tools/migrations/<這支> → 倉庫根
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.adapter import fact_audience_index  # noqa: E402
from agent.brain.runtime.memory import attention_field  # noqa: E402


def inventory(db_path: pathlib.Path) -> dict:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        facts = db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        with_audience = db.execute(
            "SELECT COUNT(*) FROM facts WHERE audience IS NOT NULL AND audience != ''").fetchone()[0]
        with_principal = db.execute(
            "SELECT COUNT(*) FROM facts WHERE principal != ''").fetchone()[0]
        indexed = fact_audience_index.count(db)
    finally:
        db.close()
    return {"facts": int(facts), "with_audience": int(with_audience),
            "with_principal": int(with_principal), "indexed_rows": indexed}


def verify(db_path: pathlib.Path, *, entity: str = "", limit: int = 8) -> dict:
    """索引 vs LIKE 的等價驗收（同一組焦點、同一時間窗）。"""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        members = [row[0] for row in db.execute(
            "SELECT DISTINCT member FROM fact_audience WHERE member != '' LIMIT 6").fetchall()]
        entities = [entity] if entity else [row[0] for row in db.execute(
            "SELECT DISTINCT entity FROM facts WHERE entity != '' LIMIT 4").fetchall()]
    finally:
        db.close()
    mismatches = []
    checked = 0
    for ent in entities:
        for member in members or ["owner"]:
            focus = {member}
            os.environ["BLUEAGENT_FOCUS_INDEX"] = "1"
            fast = attention_field.focus_rows(db_path, ent, focus)
            os.environ["BLUEAGENT_FOCUS_INDEX"] = "0"
            slow = attention_field.focus_rows(db_path, ent, focus)
            os.environ.pop("BLUEAGENT_FOCUS_INDEX", None)
            fast_ids = [row["id"] for row in fast]
            slow_ids = [row["id"] for row in slow]
            checked += 1
            if fast_ids != slow_ids:
                mismatches.append({"entity": ent, "member": member,
                                   "fast": fast_ids[:limit], "slow": slow_ids[:limit]})
    return {"checked": checked, "mismatches": mismatches}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/memory/facts.db")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true", default=True)
    parser.add_argument("--entity", default="")
    args = parser.parse_args()
    path = pathlib.Path(args.db)
    if not path.exists():
        print(f"找不到 {path}")
        return 2
    before = inventory(path)
    print(f"盤點：facts {before['facts']}／有受眾 {before['with_audience']}／"
          f"有 principal {before['with_principal']}／索引現有 {before['indexed_rows']} 列")
    print(f"時間：{time.strftime('%Y-%m-%d %H:%M')}　db={path}（{path.stat().st_size / 1e6:.1f} MB）")
    if not args.apply:
        print("（只讀盤點；要寫入請加 --apply）")
        return 0
    db = sqlite3.connect(str(path))
    try:
        result = fact_audience_index.backfill(db)
    finally:
        db.close()
    after = inventory(path)
    print(f"回填完成：掃 {result['scanned']} 條、寫 {result['written']} 列、索引共 {after['indexed_rows']} 列")
    if args.verify:
        report = verify(path, entity=args.entity)
        bad = report["mismatches"]
        print(f"等價驗收：查了 {report['checked']} 組（entity×member），不一致 {len(bad)} 組")
        for item in bad[:5]:
            print(f"  ✗ {item}")
        if bad:
            return 1
    print("✓ 索引與 LIKE 掃描結果一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
