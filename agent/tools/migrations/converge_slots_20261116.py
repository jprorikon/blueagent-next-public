#!/usr/bin/env python3
"""存量回溯：單值槽位收成一條現值（owner 2026-11-16「同一槽位保持一條，按時間關係刷新」）。

新寫入已經由 `add()` 自己收（`brain/memory/supersede.py::settle_slot`）。這支處理**已經在庫裡**的碎片：
同一個 `(entity, game, slot)` 有兩條以上現值的「狀態／稱呼」事實 ⇒ 留事件時間最新的那一條，
其餘標成「已被它取代」（不刪，as-of 還查得到）。

用法：
    # 只看（預設）：SQLite 以 mode=ro 開啟，不寫任何檔
    python3.12 agent/tools/migrations/converge_slots_20261116.py --data <data>
    # 真的改：先停服務（同一個 data/ 只能有一個行程寫），會先備份 facts.db 與 facts.jsonl
    python3.12 agent/tools/migrations/converge_slots_20261116.py --data <data> --apply

`--apply` 走 `SqliteMemory.supersede()`：索引改指針，正本 `facts.jsonl` **追加**一條操作記錄
（不改寫既有行）⇒ 之後重建索引結果一樣。跑兩次＝跑一次（第二次沒有東西可收）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sqlite3
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.brain.memory.facts import ADDRESS_SLOT  # noqa: E402

#: 「現值」＝沒被取代、也還沒過期；「單值」＝狀態或稱呼（與 `facts.single_valued` 同一條）
QUERY = """
SELECT id, entity, COALESCE(game, 0) AS game, slot, key, text, kind,
       CASE WHEN valid_from > 0 THEN valid_from ELSE created_at END AS happened
FROM facts
WHERE slot != '' AND superseded_by = '' AND key != ''
  AND (valid_to = 0 OR valid_to > ?)
  AND (LOWER(kind) = 'state' OR slot = ?)
ORDER BY entity, game, slot, happened, id
"""


def find_groups(db: sqlite3.Connection, *, now: float) -> list[dict]:
    """回每個要收的槽位：`{entity, game, slot, keep, retire[]}`（`keep` 是時間最新的那條）。"""
    buckets: dict[tuple, list[dict]] = {}
    for row in db.execute(QUERY, (float(now), ADDRESS_SLOT)):
        item = {name: row[name] for name in row.keys()}
        buckets.setdefault((item["entity"], int(item["game"]), item["slot"]), []).append(item)
    groups = []
    for (entity, game, slot), rows in buckets.items():
        if len(rows) < 2:
            continue
        keep = max(rows, key=lambda item: (float(item["happened"] or 0), item["id"]))
        groups.append({"entity": entity, "game": game, "slot": slot, "keep": keep,
                       "retire": [item for item in rows if item["id"] != keep["id"]]})
    return groups


def scan(db_path: pathlib.Path, *, now: float | None = None) -> list[dict]:
    """只讀掃描（`mode=ro`）。"""
    db = sqlite3.connect(f"{pathlib.Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        return find_groups(db, now=float(now if now is not None else time.time()))
    finally:
        db.close()


def apply(data_dir: pathlib.Path, *, now: float | None = None, backup: bool = True) -> dict:
    """把每個槽位收成一條。回 `{groups, retired, backups}`。"""
    from agent.adapter.memory_sqlite import SqliteMemory
    from agent.brain.memory.fact_store import FactStore

    base = pathlib.Path(data_dir) / "memory"
    db_path, jsonl = base / "facts.db", base / "facts.jsonl"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backups = []
    if backup:
        for path in (db_path, jsonl):
            if path.is_file():
                target = path.with_name(f"{path.name}.bak-slots-{stamp}")
                shutil.copy2(path, target)
                backups.append(str(target))
    groups = scan(db_path, now=now)
    memory = SqliteMemory(db_path, store=FactStore(jsonl), seed=False)
    retired = 0
    try:
        for group in groups:
            retired += int(memory.supersede(
                entity=group["entity"], keys=[item["key"] for item in group["retire"]],
                new_key=group["keep"]["key"], at=float(group["keep"]["happened"] or 0)) or 0)
    finally:
        memory.close()
    return {"groups": len(groups), "retired": retired, "backups": backups}


def report(groups: list[dict], *, limit: int = 20) -> str:
    by_entity: dict[str, int] = {}
    for group in groups:
        by_entity[group["entity"]] = by_entity.get(group["entity"], 0) + len(group["retire"])
    lines = ["# 單值槽位存量收斂（只讀盤點）",
             f"- 有兩條以上現值的槽位：{len(groups)} 個",
             f"- 會被收掉的事實：{sum(len(group['retire']) for group in groups)} 條",
             f"- 按 entity：{json.dumps(by_entity, ensure_ascii=False)}", ""]
    for group in sorted(groups, key=lambda item: -len(item["retire"]))[:limit]:
        lines.append(f"## {group['entity']}／game={group['game']}／{group['slot']}")
        lines.append(f"  留：{group['keep']['text'][:80]}")
        for item in group["retire"][:8]:
            lines.append(f"  收：{item['text'][:80]}")
        if len(group["retire"]) > 8:
            lines.append(f"  …另 {len(group['retire']) - 8} 條")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", default=str(ROOT / "data"))
    parser.add_argument("--apply", action="store_true", help="真的改（先停服務；會先備份）")
    parser.add_argument("--limit", type=int, default=20, help="報告裡列幾個槽位")
    args = parser.parse_args(argv)
    data = pathlib.Path(args.data)
    db_path = data / "memory" / "facts.db"
    if not db_path.is_file():
        print(f"找不到 {db_path}", file=sys.stderr)
        return 2
    sys.stdout.write(report(scan(db_path), limit=args.limit))
    if args.apply:
        result = apply(data)
        print(f"已收：{result['groups']} 個槽位／{result['retired']} 條；備份：{result['backups']}")
    else:
        print("（只讀。要真的改：先停服務，再加 --apply）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
