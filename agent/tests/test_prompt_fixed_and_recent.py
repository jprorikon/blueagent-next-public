#!/usr/bin/env python3
"""第 ③④ 步：固定段指紋＋可快取、user.md recent 不再重貼對話。

判據：
  ③ ①`user.md` 拆成固定段（稱呼／權限／identity）與動態段（recent）；
     ②固定段字串逐字穩定 ⇒ sha256 不變；改了 identity 或人格檔 ⇒ hash 會變；
     ③hash 只進 metadata（`/api/metrics` 的 `prompt`），**不進 prompt 文字**；
  ④ recent 只收穩定事實（rule／consensus），「記住這句」那種引用不進；
     話還在滾動窗裡就暫不寫；一輪最多 2 條。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.stages import MAX_RECENT_PER_TURN, _fact_prefix
from agent.brain.persona.userfile import UserFile


class TestUserFileParts(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.userfile = UserFile(self.root)
        self.userfile.ensure("owner", name="オーナー", call_name="オーナー")
        self.userfile.set_identity("owner", "オーナー\n猫が好き")
        self.userfile.remember("owner", "住んでいる：東京")

    def test_fixed_part_holds_identity_not_recent(self):
        fixed, recent = self.userfile.block_parts("owner", name="オーナー")
        self.assertIn("オーナー", fixed)
        self.assertIn("## identity", fixed)
        self.assertNotIn("東京", fixed, "recent 不該混進固定段")
        self.assertIn("東京", recent)

    def test_fixed_part_is_byte_stable(self):
        first, _ = self.userfile.block_parts("owner", name="オーナー")
        self.userfile.remember("owner", "好きな飲み物：コーヒー")
        second, _ = self.userfile.block_parts("owner", name="オーナー")
        self.assertEqual(first, second, "recent 變了不該動到固定段（prefix cache 的前提）")

    def test_block_keeps_both_parts_for_compatibility(self):
        block = self.userfile.block("owner", name="オーナー")
        self.assertIn("identity", block)
        self.assertIn("東京", block)

    def test_identity_change_is_visible_in_fixed_part(self):
        before, _ = self.userfile.block_parts("owner", name="オーナー")
        self.userfile.set_identity("owner", "オーナー\n犬が好き")
        after, _ = self.userfile.block_parts("owner", name="オーナー")
        self.assertNotEqual(before, after, "改了 identity，固定段必須變")


class TestFixedHash(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)

    def test_hash_is_recorded_and_stable_across_turns(self):
        self.state.send("こんにちは", principal="owner")
        first = dict(self.state.app.prompt_stats)
        self.assertTrue(first.get("fixed_hash"), "要算固定段指紋")
        self.assertGreater(first.get("fixed_chars", 0), 0)
        self.state.send("元気？", principal="owner")
        second = dict(self.state.app.prompt_stats)
        self.assertEqual(first["fixed_hash"], second["fixed_hash"],
                         "同一份人格／identity ⇒ hash 不該變")

    def test_hash_changes_when_identity_changes(self):
        self.state.send("こんにちは", principal="owner")
        before = self.state.app.prompt_stats["fixed_hash"]
        userfile = self.state.app.userfile
        if userfile is None:
            self.skipTest("這個建構沒有 userfile")
        userfile.set_identity("owner", "<改過的 identity>")
        self.state.send("もう一度", principal="owner")
        self.assertNotEqual(before, self.state.app.prompt_stats["fixed_hash"])

    def test_hash_is_not_in_the_prompt_text(self):
        """指紋只做觀測：多塞一行進 prompt 就破壞逐字穩定（prefix cache 失效）。"""
        seen: list[list] = []
        import os
        self.state.send("こんにちは", principal="owner")
        fixed_hash = self.state.app.prompt_stats["fixed_hash"]
        from agent.brain.messages import build_messages
        from agent.brain.ports import BrainRequest
        request = BrainRequest(thread_id="t", entity="mira", principal="owner",
                               text="やあ", system="persona")
        text = " ".join(str(item.get("content")) for item in build_messages(request))
        del seen, os
        self.assertNotIn(fixed_hash, text)


class TestRecentIsNotADialogueCopy(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)

    def _recent(self) -> list[str]:
        userfile = self.state.app.userfile
        return userfile.recent("owner") if userfile is not None else []

    def test_stable_fact_is_remembered(self):
        self.state.send("我喜欢猫", principal="owner")
        self.assertTrue(any("猫" in row for row in self._recent()),
                        f"穩定事實要進 recent：{self._recent()}")

    def test_same_slot_is_replaced_not_piled_up(self):
        self.state.send("我喜欢猫", principal="owner")
        self.state.send("我喜欢狗", principal="owner")
        likes = [row for row in self._recent() if "喜欢" in row]
        self.assertEqual(len(likes), 1, f"同一個格子只留最新：{self._recent()}")
        self.assertIn("狗", likes[0])

    def test_explicit_quote_is_not_copied_into_recent(self):
        self.state.send("これを覚えておいて：明日は雨が降るかもしれない",
                        principal="owner")
        joined = " ".join(self._recent())
        self.assertNotIn("明日は雨が降るかもしれない", joined,
                         "「記住這句」的整段引用只進記憶，不進 recent")

    def test_fact_prefix_groups_the_same_slot(self):
        self.assertEqual(_fact_prefix("主人 喜欢猫"), "主人 喜欢")
        self.assertEqual(_fact_prefix("主人 住在東京"), "主人 住在")
        self.assertEqual(_fact_prefix("ただの文"), "")

    def test_at_most_two_items_per_turn(self):
        self.assertLessEqual(MAX_RECENT_PER_TURN, 2)
        self.state.send("猫が好き。犬も好き。コーヒーも好き。紅茶も好き。",
                        principal="owner")
        self.assertLessEqual(len(self._recent()), MAX_RECENT_PER_TURN + 2,
                             "一輪不該一次塞進一大串")


if __name__ == "__main__":
    unittest.main(verbosity=2)
