#!/usr/bin/env python3
"""立刻跑一輪背景提煉（不用等 worker 的 10 分鐘）。

給驗收與排錯用：直接對 `data/` 的 raw ＋ facts.db 跑，印出這輪的帳
（掃了幾個 session、入檔幾條、跳過幾條、錯誤）。**不啟 worker**。

用法：
    python3 agent/tools/distill_now.py                      # 預設 3 個 session 上限
    python3 agent/tools/distill_now.py --max-sessions 10 --min-conf 0.6
    python3 agent/tools/distill_now.py --dry-run            # 只列 session，不呼模型、不寫檔

憑據沿用 `BLUEAGENT_ENV_FILE`（或環境變數 `DEEPSEEK_API_KEY`）。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent.adapter.memory_sqlite import SqliteMemory   # noqa: E402
from agent.brain.compose.entities import build_entities           # noqa: E402
from agent.brain.runtime.distill import Distiller              # noqa: E402
from agent.brain.runtime.rawlog import RawLog                  # noqa: E402


def load_env_file(path: str) -> None:
    if not path:
        return
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=str(ROOT / "data"))
    parser.add_argument("--entity", default="")          # 空白＝全部實體
    parser.add_argument("--max-sessions", type=int, default=3)
    parser.add_argument("--min-conf", type=float, default=0.6)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--window", action="append", default=[],
                        help="只跑指定窗口（可重複；補資料時用）")
    parser.add_argument("--env-file", default=os.environ.get("BLUEAGENT_ENV_FILE", ""))
    args = parser.parse_args()
    load_env_file(args.env_file)

    data = pathlib.Path(args.data)
    registry = build_entities()
    names = {entity.id: str(getattr(entity, "name", entity.id)) for entity in registry.all()}
    entities = [args.entity] if args.entity else list(names)
    memory = SqliteMemory(data / "memory" / "facts.db", seed=False,
                          entity_ids=entities)
    raw = RawLog(data / "raw")
    distiller = Distiller(memory=memory, raw_log=raw, entities=entities,
                          min_conf=args.min_conf, max_sessions=args.max_sessions,
                          names=names)
    if args.dry_run:
        rows = []
        for entity in entities:
            for session in distiller.sessions(entity):
                keep, reason = distiller.keep_by_rules(session)
                rows.append({"entity": entity, "key": session["key"],
                             "turns": session["turns"], "keep": keep, "reason": reason})
        print(json.dumps({"dry_run": True, "sessions": rows}, ensure_ascii=False, indent=2))
        return 0
    result = distiller.run_once(only_keys=args.window or None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
