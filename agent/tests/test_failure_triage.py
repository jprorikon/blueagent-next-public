#!/usr/bin/env python3
"""失敗的三分法（owner 2026-11-03 拍板 ①）。

判據：
  ① `no_source`（環境沒有）／`wrong_path`（路走錯）／`goal_invalid`（不該再做）判得對，
     且判定只看 trace 與次數，不看模型怎麼說；
  ② 分類詞與「走不通的路」落進 `activity.meta`（重啟後還在），下一步 prompt 會帶「換做法」；
  ③ `goal_invalid` ⇒ 活動收工（FAILED ＋ 台帳記一次失敗），不是 completed；
  ④ 另外兩類 ⇒ 這一步照實記「沒做成」往前走，不再整件事卡住等人。
"""

from __future__ import annotations

import pathlib
import sys
import threading
import types
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))   # 倉庫根

from agent.brain.domain.activity import Activity, ActivityStatus  # noqa: E402
from agent.brain.runtime.inner.failure import (  # noqa: E402
    KINDS, classify, hint_for, is_transient, record, summary)
from agent.brain.runtime.life_loop import LifeLoop  # noqa: E402
from agent.brain.runtime.life_step import _failure_hint, make_step_fn  # noqa: E402
from prompt_lang import chinese_hits  # noqa: E402


def _activity(failures: int = 0) -> Activity:
    activity = Activity.create("mira", "記事を調べる", ["調べる", "まとめる"])
    if failures:
        activity.meta["failures"] = failures
    return activity


class _Memory:
    def add(self, *args, **kwargs):
        return None


class _Entities:
    def get(self, entity_id):
        return types.SimpleNamespace(id=entity_id, name="ミラ")


class TestClassify(unittest.TestCase):
    def test_three_kinds_are_declared(self):
        self.assertEqual(KINDS, ("no_source", "wrong_path", "goal_invalid"))

    def test_success_has_no_kind(self):
        self.assertEqual(classify(failed=False, reason=""), "")

    def test_no_worker_is_goal_invalid(self):
        """沒有工作迴路 ⇒ 再試一百次也一樣。"""
        self.assertEqual(classify(failed=True, reason="no_worker", has_worker=False),
                         "goal_invalid")

    def test_too_many_failures_escalates(self):
        self.assertEqual(classify(failed=True, reason="empty", failures=2, max_attempts=3),
                         "goal_invalid")
        self.assertEqual(classify(failed=True, reason="empty", failures=1, max_attempts=3),
                         "no_source")

    def test_all_calls_denied_is_wrong_path(self):
        """被規則擋下來（重複呼叫／步內預算）⇒ 路走錯，不是世界沒東西。"""
        trace = [{"name": "mcp_firecrawl_firecrawl_search", "ok": False, "error": "duplicate_in_step"},
                 {"name": "mcp_firecrawl_firecrawl_search", "ok": False, "error": "step_call_budget"}]
        self.assertEqual(classify(failed=True, reason="no_result", attempted=trace),
                         "wrong_path")

    def test_calls_went_out_but_empty_is_no_source(self):
        trace = [{"name": "mcp_firecrawl_firecrawl_search", "ok": True, "preview": ""}]
        self.assertEqual(classify(failed=True, reason="no_result", attempted=trace),
                         "no_source")

    def test_no_trace_at_all_is_no_source(self):
        """連工具都沒叫、模型也沒吐 ⇒ 當「這一步做不出來」，換入口再寫，不升級。"""
        self.assertEqual(classify(failed=True, reason="empty", attempted=[]), "no_source")


class TestRecord(unittest.TestCase):
    def test_transient_does_not_count_against_her(self):
        """端點抖一下（TimeoutError）⇒ 留痕跡，但不累計、不進 blocked、不升級。"""
        activity = _activity(failures=2)
        self.assertEqual(record(activity, "no_source", step_text="調べる", transient=True),
                         "continue")
        self.assertEqual(activity.meta["failures"], 2)          # 沒被加上去
        self.assertEqual(activity.meta["transient_failures"], 1)
        self.assertNotIn("abort", activity.meta)
        self.assertNotIn("blocked_routes", activity.meta)

    def test_is_transient_recognises_infra_errors(self):
        self.assertTrue(is_transient("call:TimeoutError"))
        self.assertTrue(is_transient("no_result", [{"name": "mcp_firecrawl_firecrawl_search", "ok": False,
                                                    "error": "connection reset by peer"}]))
        self.assertFalse(is_transient("no_result", [{"name": "mcp_firecrawl_firecrawl_search", "ok": True}]))
        self.assertFalse(is_transient("empty", []))

    def test_writes_kind_and_counts(self):
        activity = _activity()
        self.assertEqual(record(activity, "no_source", step_text="調べる"), "continue")
        self.assertEqual(activity.meta["failure_kind"], "no_source")
        self.assertEqual(activity.meta["failures"], 1)
        self.assertEqual(activity.meta["blocked_routes"], ["調べる"])
        self.assertNotIn("abort", activity.meta)

    def test_blocked_routes_dedupe_and_cap(self):
        activity = _activity()
        for step in ["a", "a", "b", "c", "d", "e", "f"]:
            record(activity, "no_source", step_text=step)
        self.assertEqual(activity.meta["blocked_routes"], ["a", "b", "c", "d", "e"])

    def test_goal_invalid_aborts(self):
        activity = _activity(failures=2)
        self.assertEqual(record(activity, "goal_invalid", step_text="まとめる"), "abort")
        self.assertEqual(activity.meta["abort"]["kind"], "goal_invalid")
        self.assertEqual(activity.meta["abort"]["failures"], 3)

    def test_summary_line(self):
        activity = _activity()
        record(activity, "wrong_path", step_text="調べる")
        self.assertIn("wrong_path", summary(activity))
        self.assertIn("調べる", summary(activity))


class TestHints(unittest.TestCase):
    def test_hints_are_japanese(self):
        """進 prompt 的文字一律日語（`tests/prompt_lang.py` 的判據）。"""
        for kind in ("no_source", "wrong_path"):
            with self.subTest(kind=kind):
                text = hint_for(kind, blocked=["前の手"])
                self.assertEqual(chinese_hits(text), [], f"{kind}: {text}")
                self.assertIn("前の手", text)

    def test_goal_invalid_has_no_hint(self):
        """收工沒有「下一步」，所以不給提示。"""
        self.assertEqual(hint_for("goal_invalid"), "")

    def test_failure_hint_carries_kind_and_routes(self):
        activity = _activity()
        record(activity, "no_source", step_text="web で新作を探す")
        text = _failure_hint(activity)
        self.assertIn("入口を変える", text)
        self.assertIn("web で新作を探す", text)
        self.assertEqual(chinese_hits(text), [], text)

    def test_two_failures_ask_for_skill_first(self):
        activity = _activity()
        activity.meta["failures"] = 2
        activity.meta["failure_kind"] = "no_source"
        self.assertIn("mcp_skills_write", _failure_hint(activity))


def _work(payload: dict):
    """假的「真的做事」：照 payload 回一份 run_step 形狀。"""
    def run(title, step, **kwargs):
        return dict(payload)
    return run


class TestStepIntegration(unittest.TestCase):
    def _step_fn(self, payload: dict):
        return make_step_fn(_Memory(), _Entities(), duration=0.0,
                            work=_work(payload), notes_root=None)

    def test_no_source_step_continues(self):
        """查空 ⇒ 這一步照實記沒做成，往前走（不再整件事卡住）。"""
        payload = {"ok": False, "text": "", "reason": "no_result",
                   "trace": [{"name": "mcp_firecrawl_firecrawl_search", "ok": True, "preview": ""}]}
        activity = _activity()
        step_fn = self._step_fn(payload)
        note = step_fn(activity, activity.current)
        self.assertIn("no_source", note)
        self.assertEqual(activity.meta["failure_kind"], "no_source")
        self.assertEqual(activity.meta["failures"], 1)
        self.assertNotIn("abort", activity.meta)

    def test_denied_calls_are_wrong_path(self):
        payload = {"ok": False, "text": "", "reason": "no_result",
                   "trace": [{"name": "mcp_firecrawl_firecrawl_search", "ok": False, "error": "duplicate_in_step"}]}
        activity = _activity()
        self._step_fn(payload)(activity, activity.current)
        self.assertEqual(activity.meta["failure_kind"], "wrong_path")

    def test_third_failure_aborts(self):
        payload = {"ok": False, "text": "", "reason": "empty", "trace": []}
        activity = _activity(failures=2)
        self._step_fn(payload)(activity, activity.current)
        self.assertEqual(activity.meta["failure_kind"], "goal_invalid")
        self.assertEqual(activity.meta["abort"]["kind"], "goal_invalid")

    def test_success_records_no_failure(self):
        payload = {"ok": True, "text": "三本の記事を読んで、判定の作り方が分かった。",
                   "trace": [{"name": "mcp_firecrawl_firecrawl_search", "ok": True}]}
        activity = _activity()
        note = self._step_fn(payload)(activity, activity.current)
        self.assertIn("三本の記事", note)
        self.assertNotIn("failure_kind", activity.meta)


class _Store:
    def save(self, activity) -> None:
        self.saved = getattr(self, "saved", 0) + 1

    def load(self, entity):
        return None

    def clear(self, entity):
        return None


class _Ledger:
    def __init__(self) -> None:
        self.done: list = []
        self.failed: list = []

    def on_done(self, activity) -> None:
        self.done.append(activity)

    def on_failed(self, activity) -> None:
        self.failed.append(activity)


def _loop(activity) -> LifeLoop:
    loop = LifeLoop.__new__(LifeLoop)          # 不跑 __init__（那會起背景執行緒）
    loop.entity = "mira"
    loop.store = _Store()
    loop._activity = activity
    loop._cond = threading.Condition()
    loop._ledger = _Ledger()
    loop._budget = None
    loop._next_activity = None
    loop._clock = lambda: 1000.0
    loop._log = lambda message: None
    return loop


class TestFinish(unittest.TestCase):
    def test_normal_end_is_completed(self):
        activity = _activity()
        loop = _loop(activity)
        loop._finish(activity)
        self.assertEqual(activity.status, ActivityStatus.DONE)
        self.assertEqual(len(loop._ledger.done), 1)
        self.assertEqual(loop._ledger.failed, [])

    def test_abort_end_is_failed_and_ledger_hangs(self):
        """三次不成 ⇒ 收工：活動 FAILED、台帳記一次失敗（不是 completed）。"""
        activity = _activity(failures=3)
        record(activity, "goal_invalid", step_text="まとめる")
        loop = _loop(activity)
        loop._finish(activity)
        self.assertEqual(activity.status, ActivityStatus.FAILED)
        self.assertEqual(loop._ledger.done, [])
        self.assertEqual(len(loop._ledger.failed), 1)
        self.assertIn("abort:goal_invalid", activity.meta["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
