#!/usr/bin/env python3
"""渲染級語言檢查：這些地方組出來的字**真的會進模型輸入**，要是日語（review 2026-10-28）。

靜態掃描（`test_prompt_language.py`）只看得到原始碼裡的字串；這裡走一次真的合成路徑：
承接摘要的說話者標籤、每日反思的素材、技能索引的標記、委任工具描述（要跟真的回執一致）、
工具錯誤（`ToolResult.error` 會原樣回到下一則 tool message）。
"""

from __future__ import annotations

import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from prompt_lang import chinese_hits

from agent.brain.compose.summary import summarize
from agent.brain.domain.episode import CutReason, Episode
from agent.brain.domain.ids import ThreadKey
from agent.brain.domain.thread import TurnRecord
from agent.brain.runtime.delegate import RECEIPT_DELIVERED, RECEIPT_DONE_WORD, RECEIPT_TAG
from agent.brain.runtime.reflection import DailyReflection
from agent.brain.runtime.inner.reminders import ReminderStore
from agent.brain.runtime.skills import Skill
from agent.brain.runtime.tools.base import ToolContext
from agent.brain.runtime.tools.delegate import DelegateTool
from agent.brain.runtime.tools.remind import MAX_PENDING, RemindTool

KEY = ThreadKey(scope="owner", entity="mira", scene="together", purpose="chat")
CTX = ToolContext(entity="mira", principal="owner", thread_id="th-1", source="app", voice=None)


class TestSpeakerLabels(unittest.TestCase):
    def test_carry_summary_without_names_is_japanese(self):
        """以前缺顯示名時寫「她／他」。"""
        turns = (TurnRecord(turn_id="t1", role="assistant", text="読書をした。", source="app", at=1.0),
                 TurnRecord(turn_id="t2", role="user", text="続きを読もう。", source="app", at=2.0))
        episode = Episode(id="ep", key=KEY, thread_id="th", index=1, reason=CutReason.IDLE,
                          started_at=1.0, ended_at=2.0, turns=turns)
        text = summarize(episode, names={})
        self.assertEqual(chinese_hits(text), [], text)
        self.assertIn("彼女：", text)
        self.assertIn("相手：", text)

    def test_reflection_material_is_japanese(self):
        """以前把她標成「我」、缺名的對方標成「對方」，再貼進 REFLECT_PROMPT。"""

        class RawLog:
            def recent(self, entity, limit=200):
                return [{"role": "assistant", "text": "今日は読書をした。", "room": ["mira", "owner"]},
                        {"text": "いいね", "room": ["mira", "owner"]}]     # 沒名字也沒 role

        with tempfile.TemporaryDirectory() as tmp:
            reflection = DailyReflection(root=tmp, memory=None, raw_log=RawLog(), entities=["mira"])
            material, _room, _works = reflection._material("mira")   # 第三個＝今天寫的作品
        self.assertEqual(chinese_hits(material), [], material)
        self.assertIn("（わたし）今日は読書をした。", material)
        self.assertIn("（相手）いいね", material)


class TestToolSurface(unittest.TestCase):
    def test_owner_only_skill_tag_is_japanese(self):
        line = Skill(name="deploy", description="デプロイする", scope="work", body="",
                     owner_only=True).line()
        self.assertEqual(chinese_hits(line), [], line)
        self.assertNotIn("僅主人", line)

    def test_delegate_description_matches_the_real_receipt(self):
        """★ 以前描述寫「【交付回执】／已交付」，真的回執是「【交付の控え】届いた」⇒ 模型認不出做完了。"""
        description = DelegateTool.schema["function"]["description"]
        self.assertEqual(chinese_hits(description), [], description)
        self.assertIn(RECEIPT_TAG, description)
        self.assertIn(RECEIPT_DONE_WORD, description)
        self.assertTrue(RECEIPT_DELIVERED.startswith(RECEIPT_TAG + RECEIPT_DONE_WORD))
        self.assertNotIn("回执", description)

    def test_too_many_reminders_error_is_japanese(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReminderStore(tmp)
            for index in range(MAX_PENDING):
                store.add("mira", f"m{index}", tick=0, after_ticks=1000 + index)
            result = RemindTool(store).run(CTX, {"note": "もう一件", "after_ticks": 5})
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("too_many_reminders"), "機器可讀的錯誤碼不變")
        self.assertNotIn("已排", result.error)
        self.assertNotIn("筆", result.error)

    def test_self_work_whitelist_reads_posts_not_activity_log(self):
        """公共活動記錄已廢除（owner 2026-11-07）：自主動作要用公共區的帖。"""
        from agent.brain.runtime.self_work import SELF_WORK_TOOLS
        self.assertIn("mcp_board_feed", SELF_WORK_TOOLS)
        self.assertIn("mcp_board_get", SELF_WORK_TOOLS)
        self.assertNotIn("activity.peek", SELF_WORK_TOOLS,
                         "活動記錄那條工具面已廢除")


if __name__ == "__main__":
    unittest.main(verbosity=2)
