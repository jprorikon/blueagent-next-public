#!/usr/bin/env python3
"""回填窗口摘要的門牌：python3 agent/tools/backfill_window_ptrs.py --data data

為什麼要它：門牌是 2026-10-25 才加的欄位，之前的摘要只有 `turn_ids`（JSON 正本裡有）。
這一支把舊產物補成「帶門牌的摘要」，**只補缺的**，且**不重算摘要**。

2026-10-26 起也**改正舊算法的門牌**：之前 `+n` 數的是原文行（一輪兩行 ⇒ n 翻倍），
現在數不同的 turn id。門牌完全由 `turn_ids` 決定，所以重算是確定的、不是猜——
已有門牌但與 `make(turn_ids)` 不同的，換成重算的那一個（`corrected=`）。

判據：
  · 門牌一定由 `turn_ids` 推出來（不猜、不由摘要文本推）；
  · 沒有 `turn_ids` 的舊檔 ⇒ 跳過並計數（它們只能靠檢索入口，不能造假門牌）；
  · 跑完用 `tools/doctor.py` 的 `window-pointers` 對覆蓋率。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1]))

from agent.brain.runtime.memory.pointer import make, resolve  # noqa: E402
from agent.brain.runtime.memory.window_store import WindowStore  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="回填窗口摘要門牌（只補缺、不重算）")
    parser.add_argument("--data", default=str(_HERE.parents[1] / "data"))
    parser.add_argument("--entity", default="", help="只回填這個角色（省略＝全部）")
    parser.add_argument("--dry-run", action="store_true", help="只算不寫")
    parser.add_argument("--no-verify", action="store_true",
                        help="跳過「門牌要真的打得開」的檢查（不建議）")
    args = parser.parse_args(argv)

    root = pathlib.Path(args.data)
    db_path = root / "memory" / "facts.db"
    windows = root / "memory" / "windows"
    if not windows.is_dir():
        print(f"沒有 {windows}")
        return 0
    store = WindowStore(db_path)
    filled = corrected = skipped = already = unverified = 0
    for entity_dir in sorted(windows.iterdir()):
        if not entity_dir.is_dir():
            continue
        entity = entity_dir.name
        if args.entity and entity != args.entity:
            continue
        for path in sorted(entity_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                skipped += 1
                continue
            current = str(record.get("ptr") or "").strip()
            ptr = make(entity, record.get("turn_ids") or [])
            if current and (not ptr or ptr == current):
                already += 1
                continue
            if not ptr:
                # 沒有 turn_ids（或全是 null）：**不造門牌**——寧可沒有，也不要錯的
                skipped += 1
                continue
            if not args.no_verify and not resolve(ptr, root / "raw").get("ok"):
                # 門牌指不到原文 ⇒ 不寫（寫了就是一個「看起來找得到」的假指針）
                unverified += 1
                continue
            record["ptr"] = ptr
            if not args.dry_run:
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(path)
                store.upsert(record)
            if current:
                corrected += 1
            else:
                filled += 1
    print(f"filled={filled} corrected={corrected} already={already} "
          f"skipped(no turn_ids)={skipped} "
          f"unverified(打不開，未寫)={unverified}"
          + ("（dry-run，未寫入）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
