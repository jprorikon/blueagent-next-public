#!/usr/bin/env python3
"""事實分類回填：給既有事實補 `kind` / `slot`（只補欄位，不改文本）。

為什麼要這支：覆蓋、過期、as-of 都依賴 `kind`（preference/state/event/rule）與 `slot`
（狀態槽位，例如「主人正在讀的書」）。回填之前線上 104 條全是空值，規則對它們不生效。

    python3 agent/tools/migrations/classify_facts.py                 # dry run（只印，不寫）
    python3 agent/tools/migrations/classify_facts.py --apply
    python3 agent/tools/migrations/classify_facts.py --apply --limit 20

成本：每 20 條一次 DeepSeek 呼叫（104 條 ≈ 6 次）。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from agent.brain.persona.core import http_call  # noqa: E402

BATCH = 8
KINDS = {"preference", "state", "event", "rule"}

PROMPT = """你是記憶分類器。下面每條是一條記憶事實（日文/中文混用）。為每條判兩個欄位：

- kind：preference（長期偏好，如喜歡什麼）｜state（會被新值取代的狀態，如正在讀哪本書、
  現在心情、最近在忙什麼）｜event（某時發生的事，如某晚一起做了什麼）｜rule（約定/規矩）
- slot：**只有 state 需要填**，用一個穩定的短標籤表示「這是哪個槽位的現值」
  （例：主人正在讀的書 / 主人現在的心情 / ミラ最近在忙的事）。其他 kind 給空字串。

只輸出 JSON 陣列，每項 {{"i": 編號, "kind": "...", "slot": "..."}}；不要解釋。
若某條看不出來，kind 給 "preference"、slot 給 ""。

事實：
{items}
"""


def load_rows(db: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    sql = ("SELECT id, text, kind, slot FROM facts WHERE kind IS NULL OR kind = '' "
           "ORDER BY id")
    rows = list(db.execute(sql))
    return rows[:limit] if limit else rows


def parse(raw: str, count: int) -> list[dict]:
    body = str(raw or "").strip()
    start, end = body.find("["), body.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        rows = json.loads(body[start:end + 1])
    except ValueError:
        return []
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            index = int(row.get("i"))
        except (TypeError, ValueError):
            continue
        if not (1 <= index <= count):
            continue
        kind = str(row.get("kind") or "").strip().lower()
        slot = str(row.get("slot") or "").strip()
        out.append({"index": index - 1,
                    "kind": kind if kind in KINDS else "preference",
                    "slot": slot if kind == "state" else ""})
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "data" / "memory" / "facts.db"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch", type=int, default=BATCH,
                        help="一次問模型幾條（思考型模型輸出會被截斷，建議 4–8）")
    args = parser.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    rows = load_rows(db, args.limit)
    if not rows:
        print("沒有需要分類的事實")
        return 0
    batch_size = max(1, int(args.batch))
    print(f"待分類：{len(rows)} 條（每批 {batch_size}）")

    call = http_call(dict(os.environ), max_tokens=3000, disable_thinking=True)
    if call is None:
        print("沒有可用的 LLM（遠端端點要 BLUEAGENT_LLM_API_KEY／DEEPSEEK_API_KEY）")
        return 2
    changed = 0
    for offset in range(0, len(rows), batch_size):
        batch = rows[offset:offset + batch_size]
        items = "\n".join(f"{index}. {row['text'][:200]}"
                          for index, row in enumerate(batch, start=1))
        try:
            raw = call(PROMPT.format(items=items))
        except Exception as exc:                              # noqa: BLE001
            print(f"  批次 {offset // batch_size + 1} 失敗：{type(exc).__name__}: {exc}")
            continue
        decisions = parse(raw, len(batch))
        if not decisions:
            print(f"  批次 {offset // batch_size + 1} 解析失敗（跳過，不亂寫）")
            continue
        for decision in decisions:
            row = batch[decision["index"]]
            print(f"  {row['id']:>4} {decision['kind']:<10} {decision['slot'][:18]:<18}"
                  f" | {row['text'][:44]}")
            changed += 1
            if args.apply:
                db.execute("UPDATE facts SET kind = ?, slot = ? WHERE id = ?",
                           (decision["kind"], decision["slot"], row["id"]))
        if args.apply:
            db.commit()

    print(f"合計分類：{changed} 條")
    if not args.apply:
        print("（dry run；加 --apply 才寫）")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
