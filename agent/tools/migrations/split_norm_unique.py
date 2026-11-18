#!/usr/bin/env python3
"""把 `facts.norm` 的全域唯一改成「同一倉唯一」（`UNIQUE (norm, entity)`）。

為什麼要動這張表（2026-11-01）：跨角色鏡像寫入要把**同一句話**落進另一個角色的倉
（`principal` 記「聽誰說的」，提示詞用轉述口吻）。舊 schema 的 `norm TEXT NOT NULL UNIQUE`
是全域唯一 ⇒ 第二個倉那一次 insert 直接 IntegrityError，那句話在另一倉根本不存在
（生產庫實測：三個倉都只有自己那份，從來沒有鏡像成功的行）。

**這支要停服務跑**（重建表要 DROP／RENAME，309MB 的庫在跑的時候鎖不住）。

    python3 agent/tools/migrations/split_norm_unique.py --data data --dry-run
    python3 agent/tools/migrations/split_norm_unique.py --data data --apply

`--apply` 會先複製一份 `facts.db.bak-<時間>`，再在**單一事務**裡：建新表 → 全量複製 →
驗行數 → DROP 舊表 → RENAME → 重建索引與 FTS。任何一步不對就整支 rollback，磁碟上的
`facts.db` 不會動。跑完自己驗一次（schema、行數、FTS 行數、抽查三條）。

⚠️ 舊庫若已經有**跨倉重複的 norm**（不可能有：舊約束擋著）⇒ 這裡不處理，直接報錯。
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import shutil
import sqlite3
import sys

COLUMNS = ("id", "text", "norm", "layer", "audience", "label", "principal", "entity",
           "consensus", "self_owned", "source", "created_at", "kind", "valid_from",
           "valid_to", "superseded_by", "supersedes", "key", "slot", "utility", "misses",
           "last_used_at", "prior", "shingles")

NEW_TABLE = """
CREATE TABLE {name} (
    id         INTEGER PRIMARY KEY,
    text       TEXT NOT NULL,
    norm       TEXT NOT NULL,
    layer      TEXT NOT NULL,
    audience   TEXT,
    label      TEXT NOT NULL DEFAULT '',
    principal  TEXT NOT NULL DEFAULT '',
    entity     TEXT NOT NULL DEFAULT '',
    consensus  INTEGER NOT NULL DEFAULT 0,
    self_owned INTEGER NOT NULL DEFAULT 0,
    source     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    kind       TEXT NOT NULL DEFAULT '',
    valid_from REAL NOT NULL DEFAULT 0,
    valid_to   REAL NOT NULL DEFAULT 0,
    superseded_by TEXT NOT NULL DEFAULT '',
    supersedes TEXT NOT NULL DEFAULT '',
    key        TEXT NOT NULL DEFAULT '',
    slot       TEXT NOT NULL DEFAULT '',
    utility    INTEGER NOT NULL DEFAULT 0,
    misses     INTEGER NOT NULL DEFAULT 0,
    last_used_at REAL NOT NULL DEFAULT 0,
    prior      REAL NOT NULL DEFAULT 0,
    shingles   TEXT NOT NULL DEFAULT '',
    UNIQUE (norm, entity)
);
"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS facts_layer ON facts(layer, created_at)",
    "CREATE INDEX IF NOT EXISTS facts_entity ON facts(entity, created_at)",
)

TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN"
    " INSERT INTO facts_shingles(id, shingles) VALUES (new.id, new.shingles); END",
    "CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN"
    " DELETE FROM facts_shingles WHERE id = old.id; END",
    "CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN"
    " UPDATE facts_shingles SET shingles = new.shingles WHERE id = new.id; END",
)


def schema_of(db: sqlite3.Connection) -> str:
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='facts'").fetchone()
    return str(row[0] if row else "")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data", help="資料目錄（下面有 memory/facts.db）")
    parser.add_argument("--apply", action="store_true", help="真的改（預設只檢查）")
    args = parser.parse_args(argv)

    path = pathlib.Path(args.data) / "memory" / "facts.db"
    if not path.exists():
        print(f"✗ 找不到 {path}", file=sys.stderr)
        return 2
    sql = schema_of(sqlite3.connect(str(path)))
    if "UNIQUE (norm, entity)" in sql:
        print(f"✓ 已經是「同一倉唯一」，不用改：{path}")
        return 0
    if "norm" not in sql or "UNIQUE" not in sql:
        print("✗ 這張表看不出舊約束，人工確認後再跑", file=sys.stderr)
        return 2

    db = sqlite3.connect(str(path), timeout=2.0)
    db.row_factory = sqlite3.Row
    present = [row[1] for row in db.execute("PRAGMA table_info(facts)")]
    keep = [name for name in COLUMNS if name in present]
    missing = [name for name in COLUMNS if name not in present]
    facts = int(db.execute("SELECT count(*) FROM facts").fetchone()[0])
    shingles = int(db.execute("SELECT count(*) FROM facts_shingles").fetchone()[0])
    bins = (db.execute("SELECT entity, count(*) FROM facts GROUP BY entity").fetchall()
            if "entity" in present else [])
    print(f"現況：facts {facts} 條／facts_shingles {shingles} 條")
    for row in bins:
        print(f"  倉 {row[0] or '(舊資料)'}: {row[1]}")
    if missing:
        print(f"  缺欄（新表會用預設值補上）：{', '.join(missing)}")
    print("約束：norm 全域唯一（要改成 UNIQUE(norm, entity)）")

    if not args.apply:
        print("\n（dry-run：沒有改任何東西；要真的改加 --apply，並先停服務）")
        return 0

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"facts.db.bak-{stamp}")
    db.close()
    shutil.copy2(path, backup)
    print(f"備份：{backup}")

    db = sqlite3.connect(str(path), timeout=5.0)
    try:
        db.execute("PRAGMA foreign_keys=OFF")
        with db:                                     # 單一事務：任何一步錯就整支 rollback
            db.execute("DROP TABLE IF EXISTS facts_new")
            db.execute(NEW_TABLE.format(name="facts_new"))
            columns = ", ".join(keep)
            db.execute(f"INSERT INTO facts_new ({columns}) SELECT {columns} FROM facts")
            copied = int(db.execute("SELECT count(*) FROM facts_new").fetchone()[0])
            if copied != facts:
                raise RuntimeError(f"複製行數不符：{copied} != {facts}")
            db.execute("DROP TABLE facts")
            db.execute("ALTER TABLE facts_new RENAME TO facts")
            for statement in INDEXES + TRIGGERS:
                db.execute(statement)
            db.execute("INSERT INTO facts_fts(facts_fts) VALUES('rebuild')")
        db.commit()
    except Exception as exc:                          # noqa: BLE001
        db.rollback()
        print(f"✗ 失敗，已 rollback（原庫未動）：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    db = sqlite3.connect(str(path))
    after_sql = schema_of(db)
    after = int(db.execute("SELECT count(*) FROM facts").fetchone()[0])
    fts = int(db.execute("SELECT count(*) FROM facts_fts").fetchone()[0])
    sample = db.execute("SELECT text, entity, key FROM facts LIMIT 3").fetchall()
    db.close()
    ok = ("UNIQUE (norm, entity)" in after_sql and after == facts and fts == facts)
    print(f"驗收：UNIQUE(norm, entity)={'✓' if 'UNIQUE (norm, entity)' in after_sql else '✗'} "
          f"行數 {after}/{facts} FTS {fts}/{facts}")
    for row in sample:
        print(f"  抽查 {row[1] or '-'} | {str(row[0])[:30]} | key={row[2] or '(空，會自動補)'}")
    print("✓ 完成" if ok else "✗ 驗收沒過，從備份還原")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
