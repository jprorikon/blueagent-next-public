#!/usr/bin/env python3
"""重建字面索引（shingles）——修好「假名被濾掉」之後的補救。

背景：`_KEEP` 舊版只留漢字／英數，把日文假名整段濾掉 ⇒「ラノベ」「ほうじ茶」「オーナー」
在字面層永遠對不上（她講日文，這條影響很大）。修好之後，**已存在的事實**要重算 shingles。

    python3 agent/tools/migrations/rebuild_shingles.py            # dry run
    python3 agent/tools/migrations/rebuild_shingles.py --apply
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from agent.brain.memory.facts import grams, normalize_text  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "data" / "memory" / "facts.db"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    path = pathlib.Path(args.db)
    if not path.exists():
        print("找不到：", path)
        return 1
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    rows = list(db.execute("SELECT id, text, shingles FROM facts"))
    changed = 0
    for row in rows:
        shingles = " ".join(sorted(grams(normalize_text(row["text"]))))
        if shingles != (row["shingles"] or ""):
            changed += 1
            if args.apply:
                db.execute("UPDATE facts SET shingles = ? WHERE id = ?", (shingles, row["id"]))
                db.execute("DELETE FROM facts_shingles WHERE id = ?", (row["id"],))
                db.execute("INSERT INTO facts_shingles (id, shingles) VALUES (?, ?)",
                           (row["id"], shingles))
    print(f"facts={len(rows)} 需重算={changed}")
    if args.apply:
        db.execute("INSERT INTO facts_fts (facts_fts) VALUES ('rebuild')")
        db.commit()
        print("✓ 已重建（含 FTS 索引）")
    else:
        print("（dry run；加 --apply 才寫）")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
