#!/usr/bin/env python3
"""一次性資料修補（2026-10-24）：① raw 原文倉去重 ② 行動日誌事實回填 entity／有效期。

用法（**先 dry-run 看數字**，再真的跑）：
    python3 agent/tools/migrations/fix_raw_and_facts_20261024.py --data <blueagent-next/data> --dry-run
    python3 agent/tools/migrations/fix_raw_and_facts_20261024.py --data <blueagent-next/data> --apply

為什麼：
  · raw：`.watermark.json` 是「讀→追加→寫」的讀改寫，互相蓋掉 ⇒ 同一 turn 被 append 多次
    （實測 9369 行只有 157 個 turn，最多一條 169 次）。程式已修（寫入側＋讀取側去重），
    但舊檔的行還是髒的，會讓提煉／向量索引重複計算。
  · facts：`activity_public` 的事實以前存成 entity=''（＝所有角色共用）、valid_to=None
    （永不過期）⇒ 她自己的話回灌給自己，別的角色也吃得到。程式已修，舊資料要在這裡
    回填 entity 與失效時間：她對人說的話（heartbeat/voice/trpg-report）**直接標過期**。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sqlite3
import time

CONVERSATIONAL = {"heartbeat", "voice", "trpg-report"}


def raw_dedupe(data: pathlib.Path, *, dry_run: bool) -> dict:
    stats = {"files": 0, "before": 0, "after": 0, "removed": 0}
    for path in sorted(data.glob("raw/*/*.jsonl")):
        rows = []
        seen: set[tuple[str, str]] = set()
        before = 0
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                before += 1
                try:
                    row = json.loads(line)
                except ValueError:
                    rows.append(line if line.endswith("\n") else line + "\n")
                    continue
                key = (str(row.get("thread") or ""), str(row.get("turn") or ""))
                if key[1]:
                    if key in seen:
                        continue
                    seen.add(key)
                rows.append(json.dumps(row, ensure_ascii=False) + "\n")
        after = len([r for r in rows if r.strip()])
        stats["files"] += 1
        stats["before"] += before
        stats["after"] += after
        stats["removed"] += before - after
        if not dry_run and before != after:
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(rows), encoding="utf-8")
            tmp.replace(path)
    return stats


def activity_entities(data: pathlib.Path) -> dict[tuple[str, str], str]:
    """(text, kind) → entity：從活動日誌反查「這句話是誰的」。"""
    out: dict[tuple[str, str], str] = {}
    for path in sorted(data.glob("logs/activity-*.jsonl")):
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                out[(str(row.get("text") or ""), str(row.get("kind") or ""))] = str(row.get("entity") or "")
    return out


def facts_backfill(data: pathlib.Path, *, dry_run: bool) -> dict:
    path = data / "memory" / "facts.db"
    stats = {"rows": 0, "attributed": 0, "expired": 0, "skipped": 0}
    if not path.is_file():
        return stats
    lookup = activity_entities(data)
    by_text: dict[str, str] = {}
    kind_of: dict[str, str] = {}
    for (text, kind), entity in lookup.items():
        by_text.setdefault(text, entity)
        kind_of.setdefault(text, kind)
    now = time.time()
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT id, text, entity, kind, valid_from, valid_to, source, "
                      "audience, created_at FROM facts WHERE source = 'activity_public'").fetchall()
    stats["rows"] = len(rows)
    updates = []
    for row in rows:
        text = str(row["text"] or "")
        entity = by_text.get(text) or ""
        kind = kind_of.get(text) or ""
        if not entity:
            stats["skipped"] += 1
            continue
        if kind in CONVERSATIONAL:
            # 她對人說的話：不算經歷 ⇒ 直接失效（保留歷史，as-of 還查得到）
            updates.append((entity, "event", float(row["valid_from"] or row["created_at"] or now),
                            float(row["valid_from"] or row["created_at"] or now), row["id"]))
            stats["expired"] += 1
        else:
            happened = float(row["valid_from"] or row["created_at"] or now)
            updates.append((entity, "event", happened, happened + 86400.0, row["id"]))
            stats["attributed"] += 1
    if not dry_run and updates:
        with db:
            db.executemany("UPDATE facts SET entity = ?, kind = ?, valid_from = ?, "
                           "valid_to = ? WHERE id = ?", updates)
    db.close()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    data = pathlib.Path(args.data).resolve()
    if not (data / "memory").is_dir():
        print(f"不是 blueagent-next 的 data 目錄：{data}")
        return 2
    dry = not args.apply
    if not dry:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = data / "backups" / f"pre-clean-{stamp}"
        backup.mkdir(parents=True, exist_ok=True)
        for name in ("memory/facts.db", "memory/facts.jsonl"):
            src = data / name
            if src.is_file():
                shutil.copy2(src, backup / pathlib.Path(name).name)
        shutil.copytree(data / "raw", backup / "raw")
        print(f"備份 → {backup}")
    print("raw:", raw_dedupe(data, dry_run=dry))
    print("facts:", facts_backfill(data, dry_run=dry))
    print("（dry-run，沒有寫入）" if dry else "（已寫入）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
