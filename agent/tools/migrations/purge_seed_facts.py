#!/usr/bin/env python3
"""清掉生產庫裡的 demo 種子事實（`source='seed'`）——owner 2026-10-23「先做 1」。

為什麼：seed 是離線 Demo 用的假資料（Alice 是主人的妹妹／Bob 負責後端／主人怕冷空調 26 度／
她在 TRPG 認識灰鴉…）。它會被她當成真實記憶講出來，檢索沒命中時還會被當「底噪」塞進 prompt。

這支腳本做三件事：
  1. 把要刪的種子事實**歸檔**成 JSON（預設 `data/memory/archive/seed-facts-<ts>.json`）；
  2. 從 SQLite `facts` 刪掉 `source='seed'` 的列；
  3. 從 `facts.jsonl` 重寫時剔除同樣那幾條（原檔先備份）。

Hindsight 那邊不用手動刪：召回候選一定要能對回本地事實（`fact_by_text`／`find_similar`），
本地列一刪，那些語義殘留就**再也進不了 prompt**。

用法：
    python3 agent/tools/migrations/purge_seed_facts.py            # 乾跑（只報數）
    python3 agent/tools/migrations/purge_seed_facts.py --apply
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
DEFAULT_ARCHIVE = ROOT / "data" / "memory" / "archive"


def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def purge_db(path: pathlib.Path, apply: bool) -> dict:
    if not path.exists():
        return {"file": str(path), "skipped": True}
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, text, layer, audience, label, principal, entity, source, created_at "
        "FROM facts WHERE source = 'seed'").fetchall()
    payload = [dict(row) for row in rows]
    if apply and payload:
        backup = path.with_suffix(path.suffix + f".bak-{stamp()}")
        shutil.copy2(path, backup)
        print(f"  備份：{backup}")
        with db:
            db.execute("DELETE FROM facts WHERE source = 'seed'")
    db.close()
    return {"file": str(path), "seed_rows": len(payload), "rows": payload}


def purge_jsonl(path: pathlib.Path, texts: set[str], apply: bool) -> dict:
    if not path.exists():
        return {"file": str(path), "skipped": True}
    lines = path.read_text(encoding="utf-8").splitlines()
    kept, dropped = [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            kept.append(line)
            continue
        if str(raw.get("source") or "") == "seed" or str(raw.get("text") or "") in texts:
            dropped += 1
            continue
        kept.append(line)
    if apply and dropped:
        backup = path.with_suffix(path.suffix + f".bak-{stamp()}")
        shutil.copy2(path, backup)
        print(f"  備份：{backup}")
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return {"file": str(path), "dropped": dropped, "remaining": len(kept)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--jsonl", default=str(DEFAULT_JSONL))
    parser.add_argument("--archive-dir", default=str(DEFAULT_ARCHIVE))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    print(f"模式={'寫入' if args.apply else '乾跑'}")
    db_result = purge_db(pathlib.Path(args.db), args.apply)
    texts = {str(row.get("text") or "") for row in db_result.pop("rows", [])}
    jsonl_result = purge_jsonl(pathlib.Path(args.jsonl), texts, args.apply)

    if db_result.get("seed_rows"):
        archive = pathlib.Path(args.archive_dir)
        if args.apply:
            archive.mkdir(parents=True, exist_ok=True)
            path = archive / f"seed-facts-{stamp()}.json"
            path.write_text(json.dumps(sorted(texts), ensure_ascii=False, indent=2),
                            encoding="utf-8")
            print(f"  歸檔：{path}")
        print(f"  將刪除的種子事實 {db_result['seed_rows']} 條：")
        for text in sorted(texts):
            print(f"    · {text[:48]}")
    print(json.dumps({"db": db_result, "jsonl": jsonl_result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
