#!/usr/bin/env python3
"""量測通話一句的實際分段耗時（STT）。

用法（容器內）：
    python3 /app/tools/_measure_call_latency.py <wav 路徑> [--runs 3]

只量 STT（不跑大腦、不寫任何狀態）⇒ 安全、可重複。
"""
from __future__ import annotations

import os
import pathlib
import sys
import time

_APP_ROOT = pathlib.Path.cwd()
if not (_APP_ROOT / "agent" / "server").is_dir():
    _APP_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_APP_ROOT))


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: _measure_call_latency.py <wav> [--runs N]")
        return 2
    path = pathlib.Path(sys.argv[1])
    runs = 3
    if "--runs" in sys.argv:
        runs = max(1, int(sys.argv[sys.argv.index("--runs") + 1]))

    data = path.read_bytes()
    print(f"wav = {path.name}  {len(data) / 1024:.1f} KB")
    print(f"engine = {os.environ.get('BLUEAGENT_STT_ENGINE')!r}  "
          f"dir = {os.environ.get('BLUEAGENT_STT_DIR')!r}  "
          f"lang = {os.environ.get('BLUEAGENT_STT_LANG')!r}")

    from agent.brain.compose.bootstrap import build_stt

    begin = time.monotonic()
    stt = build_stt()
    print(f"build_stt = {(time.monotonic() - begin) * 1000:.0f} ms（模型載入，只算一次）")
    if stt is None:
        print("build_stt 回 None ⇒ 沒有可用的 STT")
        return 2

    lang = os.environ.get("BLUEAGENT_STT_LANG", "")
    times = []
    for index in range(runs):
        started = time.monotonic()
        result = stt.transcribe(data, language=lang, suffix=".wav")
        elapsed = (time.monotonic() - started) * 1000
        times.append(elapsed)
        print(f"  run {index + 1}: {elapsed:6.0f} ms  ok={getattr(result, 'ok', '?')}  "
              f"text={str(getattr(result, 'text', ''))[:44]!r}")

    times.sort()
    print(f"\nSTT 中位數 ≈ {times[len(times) // 2]:.0f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
