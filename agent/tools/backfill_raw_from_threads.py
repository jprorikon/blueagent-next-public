#!/usr/bin/env python3
"""把 `threads.json` 裡**還在窗口內、但沒落進 raw** 的輪次補回去（2026-11-01）。

背景（實跑查出來的）：`rawlog.sync_thread()` 舊版水準只記 `turn_id`，而一輪有兩條記錄
（user／assistant 共用同一個 `turn_id`）；segment 一旦被封存搬走，水準那個 id 就不在
segment 裡 ⇒ 舊版直接 `turns = []` **整條線永久停寫**。nori 的 11:29–11:45 九輪就是這樣
丟的，裡面有「池袋あたりに電器店まわりでもしようかと」——於是主動訊息只記得「午後は出る」，
一路問「どこへ出かけるんですか」。

修好 `sync_thread` 只保證**以後**不再丟；已經丟的要靠這支補。

用法：
    python3 agent/tools/backfill_raw_from_threads.py --data data              # 只印（預設 dry-run）
    python3 agent/tools/backfill_raw_from_threads.py --data data --apply      # 真的寫
    python3 agent/tools/backfill_raw_from_threads.py --data data --entity nori --apply

判據（`--apply` 之後）：
  · 每個 entity 的「缺」變成 0；
  · `grep 池袋 data/raw/nori/2026-11.jsonl` 命中；
  · 再跑一次 ⇒ 缺 0、寫 0（冪等）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from agent.brain.runtime.rawlog import AT_SUFFIX, RawLog  # noqa: E402


def _turn_key(thread_id: str, turn: dict) -> tuple[str, str, float]:
    return (str(thread_id), str(turn.get("turn") or ""),
            round(float(turn.get("at") or 0.0), 6))


#: `threads.json` 只留到小數 1 位、raw 是完整 float ⇒ 比對要給容差（同一輪的兩條差 7 秒，不會誤配）
AT_TOLERANCE = 2.0


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _existing(raw: RawLog, entity: str) -> dict[str, list[float]]:
    """raw 裡已有的 `thread → [at, …]`。

    ⚠️ **不要拿 `turn` 當比對鍵**：舊資料的 raw 有一堆 `"turn": None`（`str(None)` 那個老坑，
    owner 那條「ミラ、聞こえる？」就是 one of them），而 `threads.json` 裡同一個 turn 有 id
    ⇒ 按 (thread, turn) 比對會把**已經在 raw 裡的**全部誤判成「缺」（實測 1392 條假缺）。
    同一輪的兩條記錄 `at` 差約 7 秒，容差 2 秒不會把 user／assistant 配錯。
    """
    known: dict[str, list[float]] = {}
    for path in sorted(raw.entity_dir(entity).glob("*.jsonl")):
        try:
            handle = path.open("r", encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                known.setdefault(str(row.get("thread") or ""), []).append(float(row.get("at") or 0.0))
    return known


def _is_known(known: dict[str, list[float]], row: dict) -> bool:
    marks = known.get(str(row.get("thread") or ""))
    if not marks:
        return False
    at = float(row.get("at") or 0.0)
    return any(abs(at - other) <= AT_TOLERANCE for other in marks)


def _wanted(thread: dict, entity: str) -> list[dict]:
    """這條線「窗口內應該在 raw 裡」的輪次（segment ∪ transcript，按 (turn, at) 去重）。"""
    key = thread.get("key") or {}
    participants = {str(who).strip() for who in (thread.get("participants") or ())}
    room = sorted(participants | {entity})
    rows: list[dict] = []
    seen: set[tuple[str, str, float]] = set()
    for chunk in ("segment", "transcript"):
        for turn in thread.get(chunk) or ():
            at = float(turn.get("at") or 0.0)
            mark = _turn_key(str(thread.get("id") or ""), turn)
            if mark in seen:
                continue
            seen.add(mark)
            rows.append({
                "at": at,
                "entity": entity,
                "turn": str(turn.get("turn") or ""),
                "thread": str(thread.get("id") or ""),
                "scene": str(key.get("scene") or ""),
                "role": str(turn.get("role") or ""),
                "speaker": str(turn.get("speaker") or ""),
                "addressed_to": str(turn.get("addressed_to") or ""),
                "source": str(turn.get("source") or ""),
                "room": room,
                "text": str(turn.get("text") or ""),
                "audio": "",
                "engage": str(turn.get("engage") or ""),
                "generation": int(key.get("generation") or 0),
            })
    rows.sort(key=lambda row: row["at"])
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data", help="資料目錄（預設 data）")
    parser.add_argument("--entity", default="", help="只補這個角色")
    parser.add_argument("--apply", action="store_true", help="真的寫（預設只印）")
    args = parser.parse_args()

    data = pathlib.Path(args.data)
    threads_path = data / "threads.json"
    if not threads_path.exists():
        print(f"✗ 找不到 {threads_path}")
        return 2
    store = _load(threads_path)
    raw = RawLog(data / "raw")

    wanted: dict[str, list[dict]] = {}
    for thread in store.get("threads") or ():
        entity = str((thread.get("key") or {}).get("entity") or "")
        if not entity or (args.entity and entity != args.entity):
            continue
        wanted.setdefault(entity, []).extend(_wanted(thread, entity))

    total_missing = 0
    written_total = 0
    for entity, rows in sorted(wanted.items()):
        rows.sort(key=lambda row: row["at"])
        known = _existing(raw, entity)
        missing = [row for row in rows if not _is_known(known, row)]
        total_missing += len(missing)
        print(f"── {entity}: 窗口內 {len(rows)} 條、raw 已有 {len(rows) - len(missing)}、缺 {len(missing)}")
        for row in missing[:5]:
            print(f"   缺 {row['at']:.0f} {row['thread']} {row['speaker'] or row['role']}: {row['text'][:40]}")
        if len(missing) > 5:
            print(f"   …（還有 {len(missing) - 5} 條）")
        if not args.apply or not missing:
            continue
        by_month: dict[pathlib.Path, list[dict]] = {}
        for row in missing:
            by_month.setdefault(raw.month_path(entity, row["at"]), []).append(row)
        raw.entity_dir(entity).mkdir(parents=True, exist_ok=True)
        blocked: list[str] = []
        for path, chunk in by_month.items():
            try:
                with path.open("a", encoding="utf-8") as handle:
                    for row in chunk:
                        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            except PermissionError:
                # 容器以 root 跑，raw 是 root:root 0664 ⇒ 主機側（986）寫不進去
                blocked.append(str(path))
            else:
                written_total += len(chunk)
        if blocked:
            print(f"  ⚠️ 這些檔沒權限寫（容器 root 寫的）：{', '.join(blocked)}")
            print("     → 在容器裡跑這支，或先 `sudo chown -R 986:980 data && chmod -R g+rwX data`")
            continue

        # 水準只往前推（同時跑著的 app 若已經寫了更新的，不要把它拉回來）
        marks = raw._load_watermark(entity)               # noqa: SLF001 - 這支就是修水準的工具
        for thread_id in {row["thread"] for row in missing}:
            newest = max((row for row in missing if row["thread"] == thread_id),
                         key=lambda row: row["at"])
            old_at = float(marks.get(thread_id + AT_SUFFIX) or 0.0)
            if newest["at"] >= old_at:
                marks[thread_id] = newest["turn"]
                marks[thread_id + AT_SUFFIX] = f"{newest['at']:.6f}"
        raw._save_watermark(entity, marks)               # noqa: SLF001

    print()
    if args.apply:
        print(f"✓ 寫入 {written_total} 條（缺 {total_missing} 條 ⇒ 應該相等）")
    else:
        print(f"（dry-run）缺 {total_missing} 條；加 --apply 才寫")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
