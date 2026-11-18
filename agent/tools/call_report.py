#!/usr/bin/env python3
"""讀最近的通話：outbox 的 kind=call 列 ➜ 對到 turns.jsonl 的同一輪 ➜ 印分段耗時。

唯讀，不起 app。容器內跑：python3 /tmp/call_report.py /app/data [筆數]
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

DATA = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/app/data")
WANT = int(sys.argv[2]) if len(sys.argv) > 2 else 6


def hm(at: float) -> str:
    return dt.datetime.fromtimestamp(at).strftime("%m-%d %H:%M:%S")


def main() -> int:
    rows = [json.loads(line) for line in
            (DATA / "outbox" / "index.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    calls = [r for r in rows if str(r.get("kind") or "") == "call"]
    print(f"通話內容列：{len(calls)} 條")
    if not calls:
        return 0

    tele = [json.loads(line) for line in
            (DATA / "logs" / "turns.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    tele.sort(key=lambda t: t.get("at") or 0)

    for row in calls[-WANT:]:
        at = float(row.get("at") or 0)
        entity = row.get("entity")
        # outbox 的 at 是「回合存檔」時間；telemetry 的 at 是起點 ⇒ 取起點在前、最接近的那一筆
        best, best_gap = None, 1e9
        for t in tele:
            if t.get("entity") != entity:
                continue
            gap = at - float(t.get("at") or 0)
            if 0 <= gap < best_gap:
                best, best_gap = t, gap
        print(f"\n── {hm(at)} {entity} ──")
        if best is None:
            print("  （對不到 telemetry）")
            continue
        print(f"  ms={best.get('ms')}  subagents_ms={best.get('subagents_ms')}  "
              f"first_token_ms={best.get('first_token_ms')}  first_audio_ms={best.get('first_audio_ms')}")
        inp = best.get("input")
        if isinstance(inp, dict):
            keys = ("audio_s", "asr_ms", "fix_ms", "judge_ms", "complete",
                    "hold_ms", "merged", "ready_ms")
            print("  input: " + "  ".join(f"{k}={inp.get(k)}" for k in keys))
        else:
            print("  input: （這一輪沒有語音那段的欄位）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
