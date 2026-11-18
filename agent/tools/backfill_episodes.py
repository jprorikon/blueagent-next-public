#!/usr/bin/env python3
"""把已封存段落（`data/episodes.jsonl`）回填進原文倉（owner 2026-10-23「補吧」）。

為什麼要這支：raw 倉是 2026-10-23 17:19 才上線，在那之前的對話只留在 episodes（封存段）。
於是「主人說他在讀小說」那三句**原文倉裡一條都沒有**——她說「沒聽過」不是檢索壞了，
是資料真的不存在。語意檢索再強也救不了沒存下來的東西。

做法：
  · 逐 episode 逐 turn 寫進 `data/raw/<entity>/<YYYY-MM>.jsonl`（append-only，跟正常寫入同格式）
  · `room` = {entity, key_parts.scope}（當時的對話房間；scope 就是對方）
  · `source` 後面加 `+episode`（回填的看得出來源）
  · 去重靠 sidecar `data/raw/<entity>/.backfilled.json`（同樣的 turn 不會寫兩次）

    python3 agent/tools/backfill_episodes.py            # dry run
    python3 agent/tools/backfill_episodes.py --apply
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent.brain.runtime.rawlog import RawLog  # noqa: E402


def load_episodes(path: pathlib.Path) -> list[dict]:
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return rows


def turn_key(row: dict, turn: dict) -> str:
    stamp = int(float(turn.get("at") or row.get("ended_at") or 0))
    return f"{row.get('thread') or ''}#{turn.get('turn') or stamp}#{stamp}"


def plan(episodes: list[dict], raw_root: pathlib.Path) -> dict:
    """回 `{entity: {"rows": [...], "seen": n, "new": n}}`（不寫檔）。"""
    out: dict[str, dict] = {}
    for row in episodes:
        entity = str(row.get("entity") or "").strip()
        if not entity:
            continue
        bucket = out.setdefault(entity, {"rows": [], "seen": 0, "new": 0})
        principal = str((row.get("key_parts") or {}).get("scope") or "").strip()
        room = sorted({entity, principal} - {""})
        scene = str((row.get("key_parts") or {}).get("scene") or "chat")
        for turn in row.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            text = str(turn.get("text") or "").strip()
            if not text:
                continue
            key = turn_key(row, turn)
            bucket.setdefault("seen_keys", set())
            if key in bucket["seen_keys"]:
                bucket["seen"] += 1
                continue
            bucket["seen_keys"].add(key)
            bucket["rows"].append({
                "at": float(turn.get("at") or row.get("started_at") or 0),
                "thread": str(row.get("thread") or ""),
                "scene": scene,
                "role": str(turn.get("role") or ""),
                "speaker": str(turn.get("speaker") or turn.get("role") or ""),
                "addressed_to": str(turn.get("addressed_to") or ""),
                "source": f"{turn.get('source') or 'episode'}+episode",
                "text": text,
                "room": room,
                "episode": str(row.get("id") or ""),
            })
            bucket["new"] += 1
    for bucket in out.values():
        bucket.pop("seen_keys", None)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--episodes", default=str(ROOT / "data" / "episodes.jsonl"))
    parser.add_argument("--raw", default=str(ROOT / "data" / "raw"))
    parser.add_argument("--since-days", type=float, default=0.0,
                        help="只回填最近 N 天（0＝全部）")
    args = parser.parse_args()

    episodes = load_episodes(pathlib.Path(args.episodes))
    if args.since_days > 0:
        cutoff = time.time() - args.since_days * 86400
        episodes = [row for row in episodes if float(row.get("ended_at") or 0) >= cutoff]
    print(f"episodes：{len(episodes)} 段")

    raw_root = pathlib.Path(args.raw)
    raw = RawLog(raw_root)
    plan_rows = plan(episodes, raw_root)

    total_new = 0
    for entity, bucket in sorted(plan_rows.items()):
        # 對已存在的原文去重（同 thread＋同 turn 已寫過就不再寫）
        existing = {(str(row.get("thread") or ""), round(float(row.get("at") or 0), 3))
                    for row in raw.recent(entity, limit=8000)}
        rows = [row for row in bucket["rows"]
                if (row["thread"], round(float(row["at"]), 3)) not in existing]
        print(f"  {entity}: 段落 {len(bucket['rows'])} 條，待寫 {len(rows)} 條")
        total_new += len(rows)
        if args.apply and rows:
            path = raw_root / entity
            path.mkdir(parents=True, exist_ok=True)
            written = 0
            for row in rows:
                month = time.strftime("%Y-%m", time.localtime(row["at"]))
                with (path / f"{month}.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                written += 1
            print(f"    ✓ 寫入 {written} 條")

    print("合計待寫：", total_new)
    if not args.apply:
        print("（dry run；加 --apply 才寫）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
