#!/usr/bin/env python3
"""告警門檻標定：拿**真數據**決定 p95 / 失敗率門檻，不要再拍數字。

跑在容器裡（能讀 `/app/data/logs/turns.jsonl` 與 `daily/`）：

    docker exec -w /app -e PYTHONPATH=/app blueagent-next \
        python3 agent/tools/alert_calibration.py --days 7

輸出：
  · 單輪延遲分佈（p50/p90/p95/p99、最大、樣本數），依 pipeline 分組
  · 語音失敗率（speech_errors / speech）
  · 單輪失敗率（error 非空 / 有字的輪）
  · 建議門檻：p95 門檻 = max(實測 p99 × 1.2, 8 s)，失敗率門檻 = max(實測 × 3, 0.15)
    （倍數的用意：門檻要在**真的壞掉**時才叫，不是在每天的正常波動裡叫）
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys


def read_rows(paths: list[pathlib.Path], *, since: float | None) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if since is not None and float(row.get("at") or 0) < since:
                continue
            rows.append(row)
    return rows


def percentiles(values: list[float]) -> dict:
    if not values:
        return {}
    ordered = sorted(values)
    def at(p: float) -> float:
        index = min(len(ordered) - 1, max(0, int(round(p / 100 * (len(ordered) - 1)))))
        return ordered[index]
    return {"n": len(ordered), "p50": round(at(50), 1), "p90": round(at(90), 1),
            "p95": round(at(95), 1), "p99": round(at(99), 1),
            "max": round(ordered[-1], 1), "mean": round(statistics.fmean(ordered), 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default="/app/data/logs")
    ap.add_argument("--days", type=float, default=7.0)
    a = ap.parse_args()

    import time
    root = pathlib.Path(a.logs)
    since = time.time() - a.days * 86400 if a.days > 0 else None
    paths = [root / "turns.jsonl"] + sorted((root / "daily").glob("turns-*.jsonl"))
    rows = read_rows(paths, since=since)
    chats = [row for row in rows if row.get("chars") is not None]
    if not chats:
        print(f"沒有可用的輪次資料（掃了 {len(paths)} 個檔、{len(rows)} 行）")
        return 1

    print(f"資料：{len(chats)} 輪（近 {a.days:g} 天，掃 {len(paths)} 個檔）\n")

    def report(label: str, subset: list[dict]) -> None:
        if not subset:
            return
        lat = percentiles([float(row.get("ms") or 0) for row in subset if row.get("ms")])
        speech = sum(int(row.get("speech") or 0) for row in subset)
        errors = sum(int(row.get("speech_errors") or 0) for row in subset)
        failed = sum(1 for row in subset if str(row.get("error") or "").strip())
        rate_speech = (errors / speech) if speech else 0.0
        rate_turn = failed / len(subset)
        print(f"[{label}] {len(subset)} 輪")
        print(f"  延遲 ms：p50={lat.get('p50')} p90={lat.get('p90')} p95={lat.get('p95')} "
              f"p99={lat.get('p99')} max={lat.get('max')}")
        print(f"  語音：{speech} 句、失敗 {errors}（{rate_speech:.1%}）")
        print(f"  單輪失敗：{failed}（{rate_turn:.1%}）")

    report("全部", chats)
    by_pipeline: dict[str, list[dict]] = {}
    for row in chats:
        by_pipeline.setdefault(str(row.get("pipeline") or "?"), []).append(row)
    for name, subset in sorted(by_pipeline.items(), key=lambda item: -len(item[1])):
        report(name, subset)

    lat = percentiles([float(row.get("ms") or 0) for row in chats if row.get("ms")])
    speech = sum(int(row.get("speech") or 0) for row in chats)
    errors = sum(int(row.get("speech_errors") or 0) for row in chats)
    failed = sum(1 for row in chats if str(row.get("error") or "").strip())
    rate_speech = (errors / speech) if speech else 0.0
    rate_turn = failed / len(chats)

    p95_gate = max(round((lat.get("p99") or 0) * 1.2 / 1000, 1), 8.0)
    speech_gate = max(round(rate_speech * 3, 2), 0.15)
    turn_gate = max(round(rate_turn * 3, 2), 0.15)
    print("\n建議門檻（可寫進 compose）：")
    print(f"  BLUEAGENT_ALERT_P95_MS={int(p95_gate * 1000)}"
          f"   （實測 p99={lat.get('p99')} ms × 1.2，下限 8 s）")
    print(f"  BLUEAGENT_ALERT_SPEECH_FAIL={speech_gate}"
          f"   （實測 {rate_speech:.1%} × 3，下限 15%）")
    print(f"  BLUEAGENT_ALERT_TURN_FAIL={turn_gate}"
          f"   （實測 {rate_turn:.1%} × 3，下限 15%）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
