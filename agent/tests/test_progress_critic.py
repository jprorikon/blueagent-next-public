#!/usr/bin/env python3
"""位移觀察者 ＋ 熔斷（owner 2026-11-03 拍板 ④，來自外部提案的 Critic／熔斷器）。

判據：
  ① 只讀、有超時、判不了就**放行**（觀察者壞掉不該讓她停下自己的工作）；
  ② 連續 3 次「沒有往前」⇒ 下一步的 prompt 強制換做法；
  ③ 連續 6 次 ⇒ 收工（`meta["abort"]`，活動 FAILED、台帳記一次失敗）；
  ④ 判詞落進 `activity.meta["progress_marks"]`（最多 5 條）⇒ 重啟後還知道卡在哪。
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
import types
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))   # 倉庫根

from agent.brain.domain.activity import Activity  # noqa: E402
from agent.brain.runtime.life_step import (  # noqa: E402
    STUCK_ABORT, STUCK_BRANCH, _failure_hint, make_step_fn)
from agent.brain.runtime.subagents import ProgressCritic, _parse_verdict  # noqa: E402
from prompt_lang import chinese_hits  # noqa: E402


def _call(moved: bool, why: str = "", *, delay: float = 0.0, raises: bool = False):
    def call(prompt: str) -> str:
        if delay:
            time.sleep(delay)
        if raises:
            raise RuntimeError("boom")
        return json.dumps({"moved": "yes" if moved else "no", "why": why})
    return call


class _Memory:
    def add(self, *args, **kwargs):
        return None


class _Entities:
    def get(self, entity_id):
        return types.SimpleNamespace(id=entity_id, name="ミラ")


class TestReview(unittest.TestCase):
    def test_no_is_reported(self):
        verdict = ProgressCritic(_call(False, "同じ調べ方を繰り返した")).review(
            activity="判定表をまとめる", step="調べる", result="さっきと同じ")
        self.assertFalse(verdict["moved"])
        self.assertEqual(verdict["why"], "同じ調べ方を繰り返した")

    def test_yes_is_reported(self):
        verdict = ProgressCritic(_call(True, "三件目で分かった")).review(
            activity="a", step="b", result="c")
        self.assertTrue(verdict["moved"])

    def test_broken_observer_lets_her_through(self):
        """觀察者拋錯 ⇒ 放行（`moved=True`），不是判她沒進步。"""
        verdict = ProgressCritic(_call(False, raises=True)).review(activity="a", step="b", result="c")
        self.assertTrue(verdict["moved"])
        self.assertEqual(verdict["error"], "timeout")

    def test_timeout_lets_her_through(self):
        critic = ProgressCritic(_call(False, "遅い", delay=0.6), timeout_s=0.15)
        started = time.time()
        verdict = critic.review(activity="a", step="b", result="c")
        self.assertTrue(verdict["moved"])
        self.assertLess(time.time() - started, 0.5)

    def test_unparsable_lets_her_through(self):
        verdict = ProgressCritic(lambda prompt: "うーん、なんとも言えない").review(
            activity="a", step="b", result="c")
        self.assertTrue(verdict["moved"])
        self.assertEqual(verdict["why"], "unparsed")

    def test_parser(self):
        self.assertTrue(_parse_verdict('{"moved": "yes", "why": "進んだ"}')["moved"])
        self.assertFalse(_parse_verdict('{"moved": "no", "why": "同じ"}')["moved"])
        self.assertTrue(_parse_verdict("yes")["moved"])
        self.assertFalse(_parse_verdict("no")["moved"])

    def test_prompt_is_japanese(self):
        critic = ProgressCritic(lambda prompt: "{}")
        prompt = critic.PROMPT.format(activity="a", step="b", result="c", marks="d")
        self.assertEqual(chinese_hits(prompt), [], prompt)


class TestCircuit(unittest.TestCase):
    def _step_fn(self, moved: bool):
        payload = {"ok": True, "text": "記事を三本読んだが、結論は前と同じ。",
                   "trace": [{"name": "mcp_firecrawl_firecrawl_search", "ok": True}]}
        work = lambda title, step, **kwargs: dict(payload)      # noqa: E731
        return make_step_fn(_Memory(), _Entities(), duration=0.0, work=work,
                            notes_root=None, critic=ProgressCritic(_call(moved, "同じ結論")))

    def test_marks_are_kept_and_reset_by_progress(self):
        activity = Activity.create("mira", "判定表をまとめる", ["a", "b", "c"])
        step_fn = self._step_fn(False)
        step_fn(activity, activity.current)
        self.assertEqual(activity.meta["stuck"], 1)
        self.assertEqual(len(activity.meta["progress_marks"]), 1)
        # 換一個「有往前」的觀察者 ⇒ 歸零
        step_fn_ok = self._step_fn(True)
        step_fn_ok(activity, activity.current)
        self.assertEqual(activity.meta["stuck"], 0)
        self.assertEqual(len(activity.meta["progress_marks"]), 2)

    def test_three_stuck_steps_force_branch(self):
        activity = Activity.create("mira", "判定表をまとめる", ["a", "b", "c"])
        step_fn = self._step_fn(False)
        for _ in range(STUCK_BRANCH):
            step_fn(activity, activity.current)
        self.assertEqual(activity.meta["stuck"], STUCK_BRANCH)
        self.assertIn("やり方を変える", _failure_hint(activity))

    def test_six_stuck_steps_abort(self):
        activity = Activity.create("mira", "判定表をまとめる", ["a", "b", "c"])
        step_fn = self._step_fn(False)
        for _ in range(STUCK_ABORT):
            step_fn(activity, activity.current)
        self.assertEqual(activity.meta["stuck"], STUCK_ABORT)
        self.assertEqual(activity.meta["abort"]["kind"], "goal_invalid")

    def test_marks_capped(self):
        activity = Activity.create("mira", "判定表をまとめる", ["a", "b", "c"])
        step_fn = self._step_fn(False)
        for _ in range(STUCK_ABORT + 2):
            step_fn(activity, activity.current)
        self.assertLessEqual(len(activity.meta["progress_marks"]), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
