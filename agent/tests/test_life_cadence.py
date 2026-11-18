#!/usr/bin/env python3
"""生活回路的「不要原地打轉」（owner 2026-10-31：「按順序做」的 P0–P3）。

實測病徵：同樣 5-7 套活動一天重跑約 55 輪、一天 ~1000 步、~680 次工具呼叫、~80 份重複作品。
三個病灶與判據：

  ① 輪換歷史只留 24 筆（≈2 小時）⇒「今天做過幾次」算不出來；現在留 48 小時，
     而且同一套一天最多 `daily_cap` 次、兩次之間至少隔 `min_repeat_h` 小時；
     全排不進去就回 None（今天收工），不是硬排一套。
  ② 一步之內可以叫 7 次搜尋；現在一步最多 2 次、同一個 (工具+參數) 只做一次。
  ③ 成品檔名帶時分＋序號 ⇒ 同一件作品重做 40 次就留 40 份；現在檔名由「日期＋活動＋步驟」決定。
"""

from __future__ import annotations

import pathlib
import tempfile
import time

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.life import Rotation, presets_for
from agent.brain.runtime.life_step import _write_note
from agent.brain.runtime.self_work import _CallBudget, run_step
from agent.brain.runtime.tools import ToolContext, ToolRegistry, ToolResult


class TestDailyCap(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="ba-cap-"))
        self.now = time.mktime((2026, 10, 31, 12, 0, 0, 0, 0, -1))

    def test_each_preset_capped_per_day_then_none(self):
        library = tuple(p for p in presets_for("aster") if not getattr(p, "once_per_day", False))
        rotation = Rotation(self.root / "life-rotation.json", daily_cap=2, min_repeat_h=0)
        picked = []
        for index in range(len(library) * 4):
            preset = rotation.pick("aster", library, now=self.now + index * 300)
            if preset is not None:
                picked.append(preset.key)
        counts = {key: picked.count(key) for key in set(picked)}
        # 「每 N 天一次」的活動（技能整理）一天只會排一次，不套用每日上限
        expected = sum(1 if getattr(p, "every_days", 0) else 2 for p in library)
        # 防自旋（2026-11-09）：閉環的要有新材料、一天最多 1/3 ⇒ 總量只會更少，不會更多
        self.assertLessEqual(len(picked), expected, f"一天總量 ≤ 套數 × 上限：{counts}")
        self.assertGreater(len(picked), 0)
        for key, count in counts.items():
            self.assertLessEqual(count, 2, key)
        self.assertIsNone(rotation.pick("aster", library, now=self.now + 8 * 3600),
                          "同一天做滿了要回 None（今天收工），不要硬排")

    def test_min_repeat_gap_keeps_it_resting(self):
        library = tuple(p for p in presets_for("aster") if not getattr(p, "once_per_day", False))
        rotation = Rotation(self.root / "life-rotation.json", daily_cap=99, min_repeat_h=3)
        first = rotation.pick("aster", library, now=self.now)
        self.assertIsNotNone(first)
        # 只過 1 分鐘：其他套還沒做過 ⇒ 排別的；全都做過一輪之後就該休息
        later = None
        for minute in range(1, 4 * 60):
            later = rotation.pick("aster", library, now=self.now + minute * 60)
            if later is None:
                break
        self.assertIsNone(later, "全部都在 3 小時內做過 ⇒ 回 None")

    def test_open_continuation_cannot_bypass_cap_or_rest(self):
        """「還開著」的那套優先接著做，但**不能**因此無視每日上限／休息間隔。

        模型幾乎每件都說「還沒弄完」⇒ 以前延續迴圈掃整個 library，
        同一套 100 分鐘內被挑 10 次（上限 2、間隔 3 小時形同虛設）。
        """
        library = tuple(p for p in presets_for("aster") if not getattr(p, "once_per_day", False))
        rotation = Rotation(self.root / "life-rotation.json", daily_cap=2, min_repeat_h=3)
        picked = []
        for index in range(10):
            now = self.now + index * 600
            preset = rotation.pick("aster", library, hour=12, now=now)
            if preset is None:
                continue
            picked.append(preset.key)
            rotation.note_close("aster", preset.key, close="つづきはまた", is_open=True, now=now + 300)
        counts = {key: picked.count(key) for key in set(picked)}
        self.assertTrue(picked, "至少要排得出一件")
        self.assertTrue(all(count == 1 for count in counts.values()),
                        f"3 小時內同一套只能一次（延續不能繞過）：{picked}")

    def test_open_continuation_still_preferred_when_ready(self):
        """休息夠、也沒做滿 ⇒ 「還開著」那套照樣優先，而且帶著上次最後一句。"""
        library = tuple(p for p in presets_for("aster") if not getattr(p, "once_per_day", False))
        rotation = Rotation(self.root / "life-rotation.json", daily_cap=2, min_repeat_h=3)
        first = rotation.pick("aster", library, hour=12, now=self.now)
        rotation.note_close("aster", first.key, close="まだ途中", is_open=True, now=self.now + 300)
        again = rotation.pick("aster", library, hour=12, now=self.now + 4 * 3600)
        self.assertEqual(again.key, first.key)
        self.assertEqual(rotation.take_continuation(), "まだ途中")

    def test_history_keeps_a_whole_day(self):
        library = presets_for("aster")
        rotation = Rotation(self.root / "life-rotation.json")
        for index in range(60):
            rotation.pick("aster", library, now=self.now + index * 300)
        rows = [row for row in rotation.history("aster")
                if float(row["at"]) >= self.now]
        self.assertGreater(len(rows), 24, "歷史不能只留 24 筆（那樣算不出今天做過幾次）")


class _GreedyBrain:
    """一口氣叫五個工具（實測她一步就幹過），然後才吐成果。"""

    def __init__(self):
        self.calls: list[str] = []

    def run_turn(self, request):
        if not self.calls:
            for index in range(5):
                self.calls.append(f"q{index}")
                yield ("tool_call", {"id": f"c{index}",
                                     "name": "mcp_firecrawl_firecrawl_scrape",
                                     "arguments": {"url": f"https://example.com/{index}"}})
            return
        yield ("done", {"text": "調べた。"})


class _Fetch:
    name = "mcp_firecrawl_firecrawl_scrape"
    schema = {"type": "function",
              "function": {"name": "mcp_firecrawl_firecrawl_scrape",
                           "description": "x", "parameters": {}}}

    def __init__(self):
        self.seen: list[str] = []

    def run(self, ctx, arguments):
        self.seen.append(str((arguments or {}).get("url") or ""))
        return ToolResult(ok=True, data={"url": arguments.get("url"), "text": "ok"})


class TestStepCallBudget(unittest.TestCase):
    def test_only_two_calls_per_step_and_no_duplicates(self):
        fetch = _Fetch()
        tools = ToolRegistry().register(fetch)
        brain = _GreedyBrain()
        result = run_step(brain, tools, entity="aster", activity="調べる", step="読む",
                          context_factory=lambda: ToolContext(entity="aster", principal="owner",
                                                              thread_id="self:aster", source="self",
                                                              voice=None, mode="work",
                                                              is_owner=True),
                          max_rounds=2)
        self.assertEqual(len(fetch.seen), 2, f"一步最多兩次工具：{fetch.seen}")
        errors = [row.get("error") for row in result["trace"]]
        self.assertIn("step_call_budget", errors)

    def test_budget_rejects_duplicate_signature(self):
        budget = _CallBudget(2)
        self.assertEqual(budget.deny_reason("notes.read", {"entity": "aster"}), "")
        self.assertEqual(budget.deny_reason("notes.read", {"entity": "aster"}), "duplicate_in_step")
        self.assertEqual(budget.deny_reason("notes.read", {"entity": "nori"}), "")
        self.assertEqual(budget.deny_reason("mcp_firecrawl_firecrawl_scrape", {"url": "u"}),
                         "step_call_budget")


class TestWorkFileNaming(unittest.TestCase):
    """同一件作品重做 ⇒ 覆蓋同一個檔（以前一天留 79–144 份重複作品）。"""

    def test_same_work_overwrites_instead_of_piling_up(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-work-")) / "notes"
        for body in ("一度目", "二度目", "三度目"):
            _write_note(root, "aster", "観測表を作る", "欄を決める", body, form="work")
        works = sorted((root.parent / "works" / "aster").glob("*.md"))
        self.assertEqual(len(works), 1, f"同名作品要覆蓋，不該堆檔：{works}")
        self.assertIn("三度目", works[0].read_text(encoding="utf-8"), "留最新那份")


if __name__ == "__main__":
    unittest.main(verbosity=2)
