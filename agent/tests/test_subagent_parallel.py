#!/usr/bin/env python3
"""子代理並行（owner 2026-10-24：「最大 5 並行」）。

背景：2026-10-24 實測一輪 14.8 s 裡有 6.4 s 卡在子代理——兩支是**序列**跑的，
每支各自 6 s 上限 ⇒ 最壞 12 s。改成「快代理先跑（餵 evidence）＋其餘並行」後，
總耗時≈最慢那一支，且 evidence 鏈路不變。

判據：
  ① 三支各睡 0.6 s ⇒ 總耗時 < 1.2 s（序列會是 ~1.8 s）
  ② 同時最多 5 支（第 6 支要等）
  ③ 慢代理仍拿得到快代理（recall）的 evidence
  ④ 超時的那支被放棄，其他支的結果照樣回來
"""

from __future__ import annotations

import pathlib
import sys
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from agent.brain.runtime.subagents import SubAgentOrchestrator  # noqa: E402


class Sleeper:
    read_only = True

    def __init__(self, name: str, seconds: float, *, fast: bool = False,
                 seen: dict | None = None) -> None:
        self.name = name
        self.seconds = seconds
        self.fast = fast
        self.seen = seen if seen is not None else {}

    def available(self) -> bool:
        return True

    def run(self, *, entity, present, text, meta=None):
        self.seen[self.name] = list((meta or {}).get("evidence") or ())
        time.sleep(self.seconds)
        return [f"{self.name} 的筆記"]


class TestParallelSubagents(unittest.TestCase):
    def test_three_agents_run_concurrently(self):
        agents = [Sleeper(f"a{i}", 0.6) for i in range(3)]
        orch = SubAgentOrchestrator(agents, timeout_s=5.0, max_agents=5)
        started = time.monotonic()
        notes = orch.think(entity="mira", present={"owner"}, text="今日はどう？")
        spent = time.monotonic() - started
        self.assertEqual(len(notes), 3)
        self.assertLess(spent, 1.2, f"三支 0.6 s 應該並行跑完，實際 {spent:.2f}s")

    def test_concurrency_capped_at_five(self):
        running = {"now": 0, "peak": 0}
        lock = threading.Lock()

        class Tracked(Sleeper):
            def run(self, *, entity, present, text, meta=None):
                with lock:
                    running["now"] += 1
                    running["peak"] = max(running["peak"], running["now"])
                time.sleep(0.25)
                with lock:
                    running["now"] -= 1
                return [f"{self.name}"]

        agents = [Tracked(f"t{i}", 0.25) for i in range(8)]
        orch = SubAgentOrchestrator(agents, timeout_s=5.0, max_agents=8, max_workers=5)
        orch.think(entity="mira", present={"owner"}, text="全部跑起來")
        self.assertLessEqual(running["peak"], 5, f"同時最多 5 支，實際 {running['peak']}")
        self.assertGreaterEqual(running["peak"], 2, "應該真的有並行")

    def test_fast_agent_feeds_evidence_to_slow_ones(self):
        seen: dict = {}

        class Fast(Sleeper):
            def run(self, *, entity, present, text, meta=None):
                return ["主人最近在讀佐々木とピーちゃん"]

        agents = [Fast("recall", 0.0, fast=True, seen=seen),
                  Sleeper("inner", 0.05, seen=seen)]
        orch = SubAgentOrchestrator(agents, timeout_s=5.0, max_agents=5)
        orch.think(entity="mira", present={"owner"}, text="小説の話しよう")
        self.assertEqual(seen.get("inner"), ["主人最近在讀佐々木とピーちゃん"],
                         "慢代理要看得到快代理的 evidence")

    def test_timeout_does_not_block_others(self):
        agents = [Sleeper("slow", 3.0), Sleeper("quick", 0.05)]
        orch = SubAgentOrchestrator(agents, timeout_s=0.5, max_agents=5)
        started = time.monotonic()
        notes = orch.think(entity="mira", present={"owner"}, text="測試逾時")
        spent = time.monotonic() - started
        self.assertLess(spent, 1.5, f"逾時不該拖住主線，實際 {spent:.2f}s")
        texts = [note.text for note in notes]
        self.assertTrue(any("時間切れ" in text for text in texts), texts)
        self.assertTrue(any("quick" in text for text in texts), texts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
