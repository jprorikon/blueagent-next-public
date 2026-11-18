#!/usr/bin/env python3
"""把「只有 inbound、沒有 outbound」的舊取代關係補成雙向（只補空的那一側）。

背景：2026-11-01 之前 `superseded_by` 存的是**原文**，而且**沒有** `supersedes` 那一側。
`split_norm_unique.py` 遷移時把原文換成門牌，指針因此接得起來，但新那一側仍空著：
`revive()`（刪掉替代者⇒舊的復活）看不到它們，走不到反向。

這支只做一件事：對每一行 `superseded_by` 非空、且它的目標行 `supersedes` 是空的，
把來源門牌併進目標行的 `supersedes`。**不改任何文字、不刪任何行**。

    python3 agent/tools/migrations/backfill_supersedes_outbound.py --data data --dry-run
    python3 agent/tools/migrations/backfill_supersedes_outbound.py --data data --apply

⚠️ 會寫 live 庫：正式跑建議在「服務停著」的窗口裡做（跟 `split_norm_unique.py` 同一個窗口）。
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import shutil
import sqlite3
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    path = pathlib.Path(args.data) / "memory" / "facts.db"
    if not path.exists():
        print(f"✗ 找不到 {path}", file=sys.stderr)
        return 2
    db = sqlite3.connect(str(path), timeout=5.0)
    db.row_factory = sqlite3.Row
    pairs = db.execute(
        "SELECT a.id AS old_id, a.key AS old_key, a.entity AS entity,"
        "       b.id AS new_id, b.supersedes AS new_supersedes"
        "  FROM facts a JOIN facts b ON b.key = a.superseded_by"
        " WHERE a.superseded_by != '' AND (b.supersedes = '' OR b.supersedes NOT LIKE '%' || a.key || '%')"
    ).fetchall()
    print(f"需要補的：{len(pairs)} 條 inbound（目標行的 supersedes 缺這個門牌）")
    for row in pairs[:5]:
        print(f"  old={row['old_key']} ({row['entity']}) → new={row['new_id']} "
              f"supersedes={row['new_supersedes'] or '(空)'}")
    if not args.apply:
        print("\n（dry-run：沒有改任何東西）")
        return 0
    if not pairs:
        print("✓ 不用補")
        return 0

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"facts.db.bak-{stamp}")
    db.close()
    shutil.copy2(path, backup)
    print(f"備份：{backup}")

    db = sqlite3.connect(str(path), timeout=10.0)
    db.row_factory = sqlite3.Row
    merged: dict[int, set[str]] = {}
    for row in pairs:
        keys = {part for part in str(row["new_supersedes"] or "").split(",") if part}
        keys.add(str(row["old_key"]))
        merged[int(row["new_id"])] = keys
    with db:
        for new_id, keys in merged.items():
            db.execute("UPDATE facts SET supersedes = ? WHERE id = ?",
                       (",".join(sorted(keys)), new_id))
    left = db.execute(
        "SELECT count(*) FROM facts a JOIN facts b ON b.key = a.superseded_by"
        " WHERE a.superseded_by != '' AND b.supersedes = ''").fetchone()[0]
    print(f"✓ 補完；還缺 outbound 的：{left}（0 才算乾淨）")
    db.close()
    return 0 if left == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
