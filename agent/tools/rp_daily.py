#!/usr/bin/env python3
"""存摺的日結（給人看的報表；**不進她的 prompt**——aster 2026-11-01 的提醒）。

owner 2026-11-01 選「先觀察一天」，這支就是那個觀察口：一天存了多少、花了多少、花了幾件、
獨白拍了幾次。**只讀，不改任何狀態**。

    python3 agent/tools/rp_daily.py --data data                 # 今天（00:00 起）
    python3 agent/tools/rp_daily.py --data data --hours 24       # 近 24 小時
    python3 agent/tools/rp_daily.py --data data --json

判據全部從檔案算：
  · `data/rp/<角色>.jsonl`     入帳／扣款（`src=activity:*` 的就是「一件活動」）
  · `data/inner/<角色>.jsonl`  獨白拍了幾次、action 分布
  · `data/mood/<角色>.json`    最近一句心情定位（只看時間，不重算）
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from agent.brain.runtime.inner.rp_ledger import RpLedger  # noqa: E402


def _rows(path: pathlib.Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def day_report(data: pathlib.Path, *, since: float, until: float) -> list[dict]:
    ledger = RpLedger(data / "rp")
    entities = sorted(path.stem for path in (data / "rp").glob("*.jsonl")
                      if not path.name.endswith(".torn")) if (data / "rp").is_dir() else []
    report = []
    for entity in entities:
        rows = [row for row in ledger._read_rows(entity)
                if since <= float(row.get("at") or 0) < until]
        deposit = sum(int(row["amount_mrp"]) for row in rows if row.get("kind") == "deposit")
        spend = sum(int(row["amount_mrp"]) for row in rows if row.get("kind") == "spend")
        spend_rows = [row for row in rows if row.get("kind") == "spend"]
        activities = {str(row.get("src")) for row in spend_rows
                      if str(row.get("src")).startswith("activity:")}
        other_spends = [row for row in spend_rows
                        if not str(row.get("src")).startswith("activity:")]
        net_events = {str(row.get("src")) for row in rows
                      if row.get("kind") == "spend" and str(row.get("src")).endswith(":net")}
        beats = [row for row in _rows(data / "inner" / f"{entity}.jsonl")
                 if since <= float(row.get("at") or 0) < until]
        actions = {key: sum(1 for row in beats if row.get("action") == key)
                   for key in ("talk", "work", "none")}
        state = ledger.status(entity)
        report.append({
            "entity": entity,
            "deposit_rp": round(deposit / 1000, 3),
            "spend_rp": round(abs(spend) / 1000, 3),
            "net_rp": round((deposit + spend) / 1000, 3),
            "activities": len(activities - net_events),
            "network_entries": len(net_events),
            "other_spends": len(other_spends),
            "balance_rp": state["balance_rp"],
            "overdraft_rp": state["overdraft_rp"],
            "beats": len(beats), "actions": actions,
            "entries": len(rows),
        })
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="存摺日結（人看的報表）")
    parser.add_argument("--data", default="data")
    parser.add_argument("--hours", type=float, default=0.0, help="近 N 小時；不給就用「今天 00:00 起」")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    data = pathlib.Path(args.data)
    now = time.time()
    if args.hours:
        since = now - float(args.hours) * 3600.0
        label = f"近 {args.hours:g} 小時"
    else:
        local = time.localtime(now)
        since = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))
        label = time.strftime("%Y-%m-%d（00:00 起）", local)
    rows = day_report(data, since=since, until=now + 1)
    if args.json:
        print(json.dumps({"since": since, "until": now, "rows": rows}, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(f"（{data}/rp 裡還沒有存摺）")
        return 0
    print(f"== 存摺日結 {label} ==")
    for row in rows:
        print(f"{row['entity']}: 存 +{row['deposit_rp']:.3f}／花 -{row['spend_rp']:.3f}"
              f"／淨 {row['net_rp']:+.3f} RP｜餘 {row['balance_rp']:+.3f}"
              f"{'（透支）' if row['overdraft_rp'] else ''}")
        print(f"    活動 {row['activities']} 件（另有 {row['network_entries']} 筆外網明細、"
              f"{row['other_spends']} 筆無出處的舊流水）"
              f"｜流水 {row['entries']} 筆｜獨白 {row['beats']} 拍"
              f"（talk {row['actions']['talk']}／work {row['actions']['work']}／none {row['actions']['none']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
