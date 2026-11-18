#!/usr/bin/env python3
"""存摺的讀取口：**唯一權威是流水**，這個工具只負責印，不算第二份。

    python3 agent/tools/rp_status.py --data data --entity mira
    python3 agent/tools/rp_status.py --data data --entity mira --json
    python3 agent/tools/rp_status.py --data data                 # 全部角色
    python3 agent/tools/rp_status.py --selftest                  # 12 項自測（真跑）

⚠️ 面板／UI 一律讀這個（或直接讀 `status()`），**不要自己 sum(deposit) - sum(spend)**——
aster 那裡的實例：面板自算漏了 `correct` 與到期，顯示 36.080 RP，實帳 0.480。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from agent.brain.runtime.inner.rp_ledger import RpLedger, selftest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="印出角色的 RP 存摺")
    parser.add_argument("--data", default="data", help="BLUEAGENT_DATA_DIR（容器內是 /app 的 data）")
    parser.add_argument("--entity", default="", help="角色 id；不給就印資料夾裡有的全部")
    parser.add_argument("--json", action="store_true", help="輸出 JSON（給程式讀）")
    parser.add_argument("--last", type=int, default=0, help="連最近 N 筆流水一起印")
    parser.add_argument("--selftest", action="store_true", help="跑 12 項自測")
    args = parser.parse_args()

    if args.selftest:
        result = selftest()
        for name, ok in result["checks"]:
            print(("PASS " if ok else "FAIL ") + name)
        print(f"{result['passed']}/{result['passed'] + result['failed']} PASS")
        return 0 if not result["failed"] else 1

    ledger = RpLedger(pathlib.Path(args.data) / "rp")
    root = ledger.root
    entities = [args.entity] if args.entity else sorted(
        path.stem for path in root.glob("*.jsonl") if not path.name.endswith(".torn")
    )
    if not entities:
        print(f"（{root} 裡還沒有存摺）")
        return 0
    out = []
    for entity in entities:
        state = ledger.status(entity)
        state.pop("_srcs", None)
        if args.last:
            rows = ledger._read_rows(entity)[-max(1, args.last):]
            state["recent"] = [{k: row.get(k) for k in ("seq", "kind", "amount_mrp", "src", "note")}
                               for row in rows]
        out.append(state)
    if args.json:
        print(json.dumps(out if len(out) > 1 else out[0], ensure_ascii=False, indent=2))
        return 0
    for state in out:
        line = (f"{state['entity']}: {state['balance_rp']:+.3f} RP"
                f"（{state['entries']} 筆，最後 seq {state['last_seq']}）")
        if state["overdraft_rp"]:
            line += f"  ⚠ 透支 {state['overdraft_rp']:.3f} RP（帳不是閘，記實即可）"
        print(line)
        kinds = {k: v for k, v in state["by_kind_mrp"].items() if v}
        if kinds:
            print("  " + "  ".join(f"{k} {v / 1000:+.3f}" for k, v in sorted(kinds.items())))
        for row in state.get("recent") or []:
            print(f"  #{row['seq']:<4} {row['kind']:<8} {row['amount_mrp'] / 1000:+8.3f}"
                  f"  {row.get('src') or ''}  {row.get('note') or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
