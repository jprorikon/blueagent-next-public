#!/usr/bin/env python3
"""把「系統提示／桌端叫醒」這類誤入事實層的記憶歸檔（不刪，只標成失效）。

背景：TRPG 的桌端叫醒文字（「【TRPG 桌 · 輪到你】席位 p5…」）被提煉成 state 記憶，
每輪都存一條，還會蓋掉真正的狀態。這支把它們 `superseded_by=cleanup:system_prompt`、
`valid_to=now` —— 不再進注入、也搜不到現值，但資料留著可回溯。

    python3 agent/tools/migrations/archive_noise_facts.py            # dry run
    python3 agent/tools/migrations/archive_noise_facts.py --apply
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from agent.brain.runtime.distill import SYSTEM_NOISE  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "data" / "memory" / "facts.db"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    rows = [row for row in db.execute("SELECT id, text, kind, slot FROM facts")]
    hits = [row for row in rows if any(marker in str(row["text"]) for marker in SYSTEM_NOISE)]
    print(f"facts={len(rows)} 命中系統提示噪音={len(hits)}")
    for row in hits:
        print(f"  {row['id']:>4} [{row['kind'] or '-':<10}] {row['slot'][:16]:<16} | {row['text'][:52]}")
    if args.apply and hits:
        stamp = time.time()
        db.executemany(
            "UPDATE facts SET superseded_by = ?, valid_to = ?, kind = 'event' WHERE id = ?",
            [("cleanup:system_prompt", stamp, row["id"]) for row in hits])
        db.commit()
        print(f"✓ 已歸檔 {len(hits)} 條（superseded_by=cleanup:system_prompt）")
    elif not args.apply:
        print("（dry run；加 --apply 才寫）")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
