#!/usr/bin/env python3
"""按角色實體分倉：把 `facts` / `facts.jsonl` 補上 `entity` 欄並填值（owner 2026-10-23）。

背景：記憶原本只有一張表，`mira` / `aster` / `nori` 的原文混在一起（只有 Hindsight 的 bank
按實體分）。要「她 ↔ aster 各按自己角度落一份」就必須讓每一條事實知道自己屬於哪個倉。

用法：

    python3 agent/tools/migrations/migrate_entity_partition.py                 # 乾跑
    python3 agent/tools/migrations/migrate_entity_partition.py --apply         # 真的寫（先備份）
    python3 agent/tools/migrations/migrate_entity_partition.py --apply --entity mira

- 補欄：`ALTER TABLE facts ADD COLUMN entity TEXT NOT NULL DEFAULT ''`
- 填值：`entity = ''` 的舊列一律填 `--entity`（預設 `mira`，即分倉前唯一的在線實體）
- jsonl：每行補 `"entity": "<entity>"`
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import shutil
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
DEFAULT_DB = ROOT / "data" / "memory" / "facts.db"
DEFAULT_JSONL = ROOT / "data" / "memory" / "facts.jsonl"


def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def migrate_db(path: pathlib.Path, entity: str, apply: bool) -> dict:
    if not path.exists():
        return {"file": str(path), "skipped": True}
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    columns = {row["name"] for row in db.execute("PRAGMA table_info(facts)").fetchall()}
    added = False
    if "entity" not in columns:
        if apply:
            backup = path.with_suffix(path.suffix + f".bak-{stamp()}")
            shutil.copy2(path, backup)
            print(f"  備份：{backup}")
            db.execute("ALTER TABLE facts ADD COLUMN entity TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE INDEX IF NOT EXISTS facts_entity "
                       "ON facts(entity, created_at)")
            db.commit()
        added = True
    total = int(db.execute("SELECT count(*) FROM facts").fetchone()[0])
    empty = total if (added and not apply) else int(
        db.execute("SELECT count(*) FROM facts WHERE entity = ''").fetchone()[0])
    if apply and empty:
        db.execute("UPDATE facts SET entity = ? WHERE entity = ''", (entity,))
        db.commit()
    db.close()
    return {"file": str(path), "added_column": added, "total": total,
            "filled": empty, "entity": entity}


def migrate_jsonl(path: pathlib.Path, entity: str, apply: bool) -> dict:
    if not path.exists():
        return {"file": str(path), "skipped": True}
    lines = path.read_text(encoding="utf-8").splitlines()
    out, filled, bad = [], 0, 0
    for line in lines:
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            bad += 1
            out.append(line)
            continue
        if not str(raw.get("entity") or ""):
            raw["entity"] = entity
            filled += 1
        out.append(json.dumps(raw, ensure_ascii=False))
    if apply and filled:
        backup = path.with_suffix(path.suffix + f".bak-{stamp()}")
        shutil.copy2(path, backup)
        print(f"  備份：{backup}")
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return {"file": str(path), "total": len(out), "filled": filled, "bad_lines": bad}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entity", default="mira")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--jsonl", default=str(DEFAULT_JSONL))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(f"entity={args.entity}　模式={'寫入' if args.apply else '乾跑'}")
    result = {"db": migrate_db(pathlib.Path(args.db), args.entity, args.apply),
              "jsonl": migrate_jsonl(pathlib.Path(args.jsonl), args.entity, args.apply)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
