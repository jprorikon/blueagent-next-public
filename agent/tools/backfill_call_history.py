#!/usr/bin/env python3
"""回填：把**已存在的**通話回合從 App 的對話紀錄裡拿掉，只留一條標記。

python3 agent/tools/backfill_call_history.py --data data [--apply]

★ 執行時機：服務停止時（`outbox/index.jsonl` 由 runtime 持有，邊跑邊改會互相覆蓋）：
    docker compose stop
    docker run --rm -v "$PWD/data:/app/data" -v "$PWD/tools:/app/tools:ro" \
        --entrypoint python3 blueagent-next:latest \
        tools/backfill_call_history.py --data /app/data --apply
    docker compose start

判據（**無歧義**，與私訊那批不同）：`source == "phone:call"` 就是通話裡的回合。
按 `entity` ＋ 間隔 <`SESSION_GAP_S` 切成「一通電話」，每通：
  · 每一列標 `kind="call"` ⇒ `/api/turns` 濾掉（內容不留）；
  · 末尾插入**一條** `kind="call-log"` 標記 ⇒ 「發生過通話」。
⚠️ 舊資料沒有 session 紀錄 ⇒ **不編造時長**，標記只寫「（通話）」。
寫入前自動備份 `outbox/index.jsonl.bak-call-<ts>`。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_APP_ROOT = pathlib.Path.cwd()
if not (_APP_ROOT / "agent" / "brain").is_dir():
    _APP_ROOT = _HERE.parents[1]
sys.path.insert(0, str(_APP_ROOT))

from agent.brain.runtime.delivery.outbox import new_turn_id  # noqa: E402

#: 同一通的相鄰兩句最遠間隔（秒）；超過就當另一通。
SESSION_GAP_S = 300.0
#: 通話回合的來源標籤。
CALL_SOURCE = "phone:call"
#: 標記的文字（舊資料不知道真實時長，不編）。
MARKER_TEXT = "（通話）"


def plan(data: pathlib.Path) -> dict:
    index_path = data / "outbox" / "index.jsonl"
    if not index_path.exists():
        return {"rows": 0, "sessions": [], "todo": []}
    rows = [json.loads(line) for line in
            index_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # 已經是標記的、或已經標過的不動
    targets = [row for row in rows
               if str(row.get("source") or "") == CALL_SOURCE
               and str(row.get("kind") or "") != "call"
               and str(row.get("kind") or "") != "call-log"]
    targets.sort(key=lambda row: float(row.get("at") or 0))

    sessions: list[list[dict]] = []
    for row in targets:
        if sessions:
            last = sessions[-1][-1]
            same = str(last.get("entity")) == str(row.get("entity"))
            close = float(row.get("at") or 0) - float(last.get("at") or 0) <= SESSION_GAP_S
            if same and close:
                sessions[-1].append(row)
                continue
        sessions.append([row])

    return {"rows": len(rows), "sessions": sessions, "targets": targets}


def apply(data: pathlib.Path, *, dry_run: bool = True) -> dict:
    data = pathlib.Path(data)
    index_path = data / "outbox" / "index.jsonl"
    result = plan(data)
    sessions = result["sessions"]
    targets = result.get("targets") or []
    summary = {
        "rows": result["rows"],
        "call_rows": len(targets),
        "sessions": len(sessions),
    }
    if dry_run or not targets:
        summary["dry_run"] = True
        summary["plan"] = [
            {"entity": str(seg[0].get("entity")),
             "rows": len(seg),
             "span": f"{_stamp(seg[0])} → {_stamp(seg[-1])}",
             "ids": [str(row.get("turn_id")) for row in seg]}
            for seg in sessions]
        return summary

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = index_path.with_name(f"index.jsonl.bak-call-{stamp}")
    backup.write_bytes(index_path.read_bytes())

    marked = {str(row.get("turn_id")) for row in targets}
    markers: dict[str, dict] = {}
    for seg in sessions:
        last = seg[-1]
        marker = {
            "turn_id": new_turn_id(),
            "at": round(float(last.get("at") or 0) + 0.001, 3),
            "principal": str(last.get("principal") or "owner"),
            "entity": str(last.get("entity") or ""),
            "thread": str(last.get("thread") or ""),
            "text": MARKER_TEXT,
            "asked": "",
            "ok": True,
            "error": "",
            "frames": 0,
            "speech": 0,
            "audio": False,
            "bytes": 0,
            "source": CALL_SOURCE,
            "kind": "call-log",
        }
        markers[str(last.get("turn_id"))] = marker

    rows = [json.loads(line) for line in
            index_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    out: list[str] = []
    for row in rows:
        turn_id = str(row.get("turn_id") or "")
        if turn_id in marked and str(row.get("source") or "") == CALL_SOURCE:
            row = dict(row)
            row["kind"] = "call"
        out.append(json.dumps(row, ensure_ascii=False))
        if turn_id in markers:
            out.append(json.dumps(markers[turn_id], ensure_ascii=False))
    index_path.write_text("\n".join(out) + "\n", encoding="utf-8")

    summary.update({"dry_run": False, "marked": len(marked), "markers": len(markers),
                    "backup": str(backup)})
    return summary


def _stamp(row: dict) -> str:
    return dt.datetime.fromtimestamp(float(row.get("at") or 0)).strftime("%m-%d %H:%M:%S")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="回填：通話回合從 App 紀錄裡拿掉，只留標記")
    parser.add_argument("--data", default=str(_APP_ROOT / "data"))
    parser.add_argument("--apply", action="store_true", help="真的寫入（預設只算不寫）")
    args = parser.parse_args(argv)
    stats = apply(pathlib.Path(args.data), dry_run=not args.apply)
    for item in stats.pop("plan", []):
        print("  PLAN", item)
    print(" ".join(f"{key}={value}" for key, value in stats.items()
                    if isinstance(value, (int, float, str))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
