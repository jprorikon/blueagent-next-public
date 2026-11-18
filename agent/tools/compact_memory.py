#!/usr/bin/env python3
"""清理向量倉孤兒並壓縮兩個庫（owner 2026-11-03「繼續清」的收尾）。

為什麼要有：facts 刪掉 26,318 條之後，`facts.db` 還是 506 MB（SQLite 不會自己還空間），
`vectors.db` 611 MB 裡也還留著那些已刪事實的向量。

順序（呼叫端負責停容器）：
  1. `vec` 表裡 kind='fact' 且 ref 已不在 facts 的 ⇒ 刪；
  2. 兩個庫各 VACUUM 一次。

    python3 agent/tools/compact_memory.py --data data        # dry-run：只印會刪多少
    python3 agent/tools/compact_memory.py --data data --apply
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys
import time


def _sizes(root: pathlib.Path) -> dict:
    out = {}
    for name in ("facts.db", "vectors.db"):
        path = root / "memory" / name
        out[name] = path.stat().st_size if path.exists() else 0
    return out


def orphan_vectors(facts: pathlib.Path, vectors: pathlib.Path) -> list[tuple[str, str, str]]:
    """回 `[(entity, ref, kind)]`：向量倉裡指向已不存在事實的條目。"""
    facts_db = sqlite3.connect(f"file:{facts}?mode=ro", uri=True)
    texts = {str(row[0]) for row in facts_db.execute("SELECT text FROM facts")}
    facts_db.close()
    vec_db = sqlite3.connect(f"file:{vectors}?mode=ro", uri=True)
    rows = [(str(entity), str(ref), str(kind)) for kind, entity, ref
            in vec_db.execute("SELECT kind, entity, ref FROM vec WHERE kind = 'fact'")]
    vec_db.close()
    return [row for row in rows if row[1] not in texts]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    root = pathlib.Path(args.data)
    facts, vectors = root / "memory" / "facts.db", root / "memory" / "vectors.db"
    if not facts.exists() or not vectors.exists():
        print(f"缺庫：{facts} / {vectors}")
        return 1
    before = _sizes(root)
    print(f"現在：facts.db {before['facts.db'] / 1e6:.0f} MB｜"
          f"vectors.db {before['vectors.db'] / 1e6:.0f} MB")
    doomed = orphan_vectors(facts, vectors)
    print(f"向量孤兒（指向已刪事實）：{len(doomed)} 條")
    if not args.apply:
        print("（dry-run：什麼都沒動。要真的清請加 --apply，**先停容器**）")
        return 0
    vec_db = sqlite3.connect(str(vectors))
    try:
        with vec_db:
            vec_db.executemany("DELETE FROM vec WHERE kind = 'fact' AND entity = ? AND ref = ?",
                               [(entity, ref) for entity, ref, _kind in doomed])
    finally:
        vec_db.close()
    for name, path in (("facts.db", facts), ("vectors.db", vectors)):
        started = time.time()
        db = sqlite3.connect(str(path))
        try:
            db.execute("VACUUM")
        finally:
            db.close()
        print(f"  VACUUM {name} 完成（{time.time() - started:.1f}s）")
    after = _sizes(root)
    print(f"清完：facts.db {after['facts.db'] / 1e6:.0f} MB｜"
          f"vectors.db {after['vectors.db'] / 1e6:.0f} MB"
          f"（省 {(sum(before.values()) - sum(after.values())) / 1e6:.0f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
