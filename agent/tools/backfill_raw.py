#!/usr/bin/env python3
"""把歷史對話回填進 raw 原文倉（`data/raw/<entity>/<YYYY-MM>.jsonl`）。

來源（兩份都在 data/）：
  · `threads.json`              —— 活線（segment／transcript 還在的輪次）
  · `archive/threads-*.jsonl`   —— 封存（segment 超量時搬走的輪次）

去重：以 (thread, turn) 為鍵，已存在於 raw 的就跳過 ⇒ 可重跑。

用法：
    python3 agent/tools/backfill_raw.py            # 乾跑（只報數）
    python3 agent/tools/backfill_raw.py --apply
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_DATA = ROOT / "data"
MONTH_FMT = "%Y-%m"


def month_key(at: float) -> str:
    import time
    stamp = time.localtime(float(at))
    return f"{stamp.tm_year:04d}-{stamp.tm_mon:02d}"


def entity_of(key: dict, fallback: str = "") -> str:
    value = str((key or {}).get("entity") or fallback or "").strip()
    return value or "unknown"


def rows_from_threads(path: pathlib.Path) -> list[dict]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows: list[dict] = []
    for thread in body.get("threads") or []:
        key = thread.get("key") or {}
        entity = entity_of(key)
        room = sorted({entity, *(thread.get("participants") or [])})
        for turn in (thread.get("segment") or thread.get("transcript") or []):
            rows.append({
                "at": float(turn.get("at") or 0.0),
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
                "generation": 0,
            })
    return rows


def rows_from_archive(folder: pathlib.Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(folder.glob("threads-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            key = item.get("key") or {}
            entity = entity_of(key, item.get("entity") or "")
            turn = item.get("turn") or {}
            rows.append({
                "at": float(turn.get("at") or 0.0),
                "entity": entity,
                "turn": str(turn.get("turn") or ""),
                "thread": str(item.get("thread") or ""),
                "scene": str(key.get("scene") or ""),
                "role": str(turn.get("role") or ""),
                "speaker": str(turn.get("speaker") or ""),
                "addressed_to": str(turn.get("addressed_to") or ""),
                "source": str(turn.get("source") or ""),
                "room": sorted({entity, *(item.get("participants") or [])}),
                "text": str(turn.get("text") or ""),
                "audio": "",
                "engage": str(turn.get("engage") or ""),
                "generation": 0,
            })
    return rows


def existing_keys(raw_root: pathlib.Path) -> set[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    for path in raw_root.glob("*/*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            seen.add((str(row.get("thread") or ""), str(row.get("turn") or "")))
    return seen


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=str(DEFAULT_DATA))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    data = pathlib.Path(args.data)
    raw_root = data / "raw"
    rows = rows_from_threads(data / "threads.json") + rows_from_archive(data / "archive")
    seen = existing_keys(raw_root) if raw_root.exists() else set()
    fresh = [row for row in rows if (row["thread"], row["turn"]) not in seen]
    per_entity = collections.Counter(str(row.get("entity") or "unknown") for row in fresh)
    written = 0
    marks_by_entity: dict[str, dict[str, str]] = {}
    if args.apply:
        for row in fresh:
            if not row["text"]:
                continue
            entity = str(row.get("entity") or "unknown")
            folder = raw_root / entity
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{month_key(row['at'] or 0.0)}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            # 同步 watermark（thread → 最後一輪），否則容器下次 persist 會把同一批再寫一遍
            marks_by_entity.setdefault(entity, {})[str(row.get("thread") or "")] = \
                str(row.get("turn") or "")
            written += 1
        for entity, marks in marks_by_entity.items():
            path = raw_root / entity / ".watermark.json"
            try:
                current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except ValueError:
                current = {}
            current.update(marks)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(current, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print(json.dumps({"模式": "寫入" if args.apply else "乾跑",
                      "來源輪次": len(rows), "已在 raw": len(seen),
                      "待補": len(fresh), "實寫": written,
                      "分佈": dict(per_entity)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
