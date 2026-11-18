#!/usr/bin/env python3
"""E4 先量再遷（架構審查 2026-11-15 §3.2-4）：`ThreadStore.persist()` 每次要多久、寫多大。

`persist()` 在**每一輪結束**（`compose/turns.py`）與開新線時跑：整份 `threads.json` 重新序列化＋原子替換。
這支量的是那一下的成本，決定要不要把 threads 遷 SQLite。

用法：
    # 合成資料（預設：60 條活線 × 片段 200 輪 × 每輪 120 字）
    python3 agent/tools/bench_threads_persist.py
    python3 agent/tools/bench_threads_persist.py --threads 200 --segment 200 --chars 200
    # 真實檔（容器內；只讀原檔，複製到暫存目錄量，不碰 data/）
    sudo docker exec blueagent-next python3 agent/tools/bench_threads_persist.py --from /app/data/threads.json
    # 模擬線上形狀：每組 key 有好幾代（每代隔 2 小時），看回收前後
    python3 agent/tools/bench_threads_persist.py --threads 20 --gens 25 --segment 3 --transcript 30

輸出一行 JSON：檔案大小、序列化與整次 persist 的 p50／p95／max（毫秒）；
`after_prune` 是對同一份資料跑一次 `ThreadStore.prune(現在)`（E4 回收器，2026-11-17）之後再量的結果。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import statistics
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.brain.domain.event import SourceClass        # noqa: E402
from agent.brain.domain.ids import ThreadKey            # noqa: E402
from agent.brain.domain.thread import TurnRecord        # noqa: E402
from agent.brain.runtime.threadstore import ThreadStore  # noqa: E402


def synthetic(store: ThreadStore, *, threads: int, segment: int, chars: int, gens: int = 1,
              transcript: int = 0) -> None:
    """`threads` 組 key × 每組 `gens` 代（每代隔 2 小時，最後一代是現在）。

    每代先記 `transcript` 輪（模擬已封片、只剩滾動窗的部分），再記 `segment` 輪（沒封片的尾巴）。
    """
    now = time.time()
    body = ("今日は観測の記録を整理して、明日の予定も少し決めた。" * 20)[:max(1, chars)]
    for index in range(threads):
        key = ThreadKey(f"p{index % 7}", ("mira", "aster", "nori")[index % 3], scene=f"s{index}")
        for gen in range(max(1, gens)):
            at = now - (max(1, gens) - 1 - gen) * 7200.0
            thread = store.attach(key, now=at, window=900.0, lane=SourceClass.INTERACTIVE).thread
            with thread.lock:
                for turn in range(transcript + segment):
                    thread.record(TurnRecord(turn_id=f"t{index}-{gen}-{turn}",
                                             role="user" if turn % 2 == 0 else "assistant",
                                             text=body, source="phone:owner",
                                             at=at - (transcript + segment - turn) * 30, speaker="owner"))
                    if turn == transcript - 1:
                        thread.segment = []
                thread.last_active_at = at


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def measure(store: ThreadStore, path: pathlib.Path, rounds: int) -> dict:
    snapshot = store.threads()
    serialize: list[float] = []
    persist: list[float] = []
    for _ in range(max(1, rounds)):
        started = time.perf_counter()
        json.dumps({"threads": [thread.to_store_dict() for thread in snapshot]}, ensure_ascii=False,
                   separators=(",", ":"))
        serialize.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        store.persist()
        persist.append((time.perf_counter() - started) * 1000)
    return {
        "threads": len(snapshot), "segment_turns": sum(len(thread.segment) for thread in snapshot),
        "bytes": path.stat().st_size,
        "serialize_ms": {"p50": round(statistics.median(serialize), 2),
                         "p95": round(percentile(serialize, 0.95), 2), "max": round(max(serialize), 2)},
        "persist_ms": {"p50": round(statistics.median(persist), 2),
                       "p95": round(percentile(persist, 0.95), 2), "max": round(max(persist), 2)},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="source", default="", help="量真實的 threads.json（會先複製到暫存目錄）")
    parser.add_argument("--threads", type=int, default=60)
    parser.add_argument("--segment", type=int, default=200, help="每條線片段幾輪（上限是 BLUEAGENT_THREAD_SEGMENT_MAX=200）")
    parser.add_argument("--chars", type=int, default=120)
    parser.add_argument("--gens", type=int, default=1, help="合成：每組 key 幾代（每代隔 2 小時）")
    parser.add_argument("--transcript", type=int, default=0, help="合成：每代已封片、只留在滾動窗裡的輪數")
    parser.add_argument("--rounds", type=int, default=30)
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="ba-bench-threads-") as folder:
        path = pathlib.Path(folder) / "threads.json"
        if args.source:
            shutil.copyfile(args.source, path)
            # 不接 raw_log、不封存：只量序列化＋寫檔；max_segment 放大，免得量的時候把片段搬走
            store = ThreadStore(path=path, max_segment=10 ** 6, archive_dir=pathlib.Path(folder) / "archive")
            mode = "file"
        else:
            store = ThreadStore(path=path, max_segment=10 ** 6, archive_dir=pathlib.Path(folder) / "archive")
            synthetic(store, threads=args.threads, segment=args.segment, chars=args.chars,
                      gens=args.gens, transcript=args.transcript)
            mode = "synthetic"
        report = {"mode": mode, **measure(store, path, args.rounds)}
        closed = store.prune(time.time())
        report["after_prune"] = {"closed": len(closed), **measure(store, path, args.rounds)}
        print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
