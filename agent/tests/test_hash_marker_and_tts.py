#!/usr/bin/env python3
"""回信裡的 `#數字` 與 TTS 唸數字（owner 2026-10-29）

三個成因、三個修法：

1. **prompt 教她寫 `#N`**：`_history_line` 在正文前面加「`#N（X 前）`」⇒ 模型學會照抄，
   連帶 TTS 把號碼念出來。
   ⇒ 修法 A：**歷史行不再加編號／時間前綴**（輪次與「多久前」另有 `clock_block` 給）。
2. **prompt 明講「歷史前面有 `#N（X 前）`」** ⇒ 她學會在正文寫編號。
   ⇒ 修法 B：`[[clock]]` 段改成用**文章**說時間，並明說正文不寫 `#數字`。
3. **TTS 邊界沒有清洗** ⇒ 就算正文殘留了 `#112`，TTS 也會念「一一二」。
   ⇒ 修法 C：`speech_text()` 在合成前把 `#N（…）` 整段去掉。

⚠️ 真值：正文裡的 `#N` **不是**儲存層污染（`threads.json` 的 transcript 是乾淨的），
而是 prompt 層產生的 —— 所以修法 A 只需要改 `_history_line`。
"""
from __future__ import annotations

import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.messages import _history_line, build_messages
from agent.brain.runtime.voice import speech_text


class TestHistoryHasNoHashPrefix(unittest.TestCase):
    """修法 A：歷史行不該帶 `#N（X 前）` 前綴。"""

    def _line(self, role: str, index: int, at: float) -> str:
        row = {"role": role, "text": "うん、おやすみ Alice。", "at": at, "index": index}
        return str(_history_line(row, group=False, names={}, moment=at + 120.0)["content"])

    def test_no_hash_index_in_history_line(self):
        out = self._line("assistant", 112, 1793260000.0)
        self.assertNotIn("#", out, "歷史行不該帶 # 編號（模型會照抄進自己的回覆）")
        self.assertNotIn("112", out)
        self.assertIn("うん、おやすみ Alice。", out)

    def test_no_time_marker_in_history_line(self):
        out = self._line("assistant", 112, 1793260000.0)
        self.assertNotIn("分前", out, "時間標記也會被模型照抄（「（いま）」）")
        self.assertNotIn("（", out)

    def test_user_line_also_clean(self):
        out = self._line("user", 111, 1793260000.0)
        self.assertEqual(out, "うん、おやすみ Alice。")

    def test_group_speaker_prefix_still_works(self):
        row = {"role": "user", "text": "ありがとう", "speaker": "guest-707",
               "at": 1793260000.0, "index": 3}
        out = _history_line(row, group=True, names={"guest-707": "Alice"}, moment=1793260010.0)
        self.assertEqual(out["content"], "Alice：ありがとう")

    def test_empty_text_dropped(self):
        row = {"role": "assistant", "text": "   ", "at": 1793260000.0, "index": 1}
        self.assertIsNone(_history_line(row, group=False, names={}, moment=1793260010.0))


class TestClockBlockDoesNotTeachHash(unittest.TestCase):
    """修法 B：`[[clock]]` 段不該教她在正文寫 `#數字`。"""

    def _clock_system_text(self) -> str:
        from agent.brain.ports import BrainRequest
        req = BrainRequest(
            thread_id="th", entity="mira", principal="guest-707", text="やあ",
            system="いま話しているのは Alice。",
            meta={"clock_block": "### いま（時間感）\n- 現在：2026-10-29（木）19:12\n"
                               "- この会話：194 往復目、前の発言はさっき"})
        # 2026-11-02：每輪易變段（含 [[clock]]）已改併進最後一條 user 訊息
        # （為了讓整份 prompt 是上一次的嚴格延伸 ⇒ 引擎前綴快取才命中）。
        # 本測只關心 clock 段的**內容**，所以去「最後一條 user 訊息」裡找。
        msgs = build_messages(req, max_history=10)
        tail = [str(m.get("content") or "") for m in msgs if m.get("role") == "user"]
        for content in reversed(tail):
            if "[[clock]]" in content:
                return content
        self.fail("組不出 [[clock]] 段")

    def test_clock_block_does_not_mention_hash_marker(self):
        text = self._clock_system_text()
        self.assertNotIn("`#N（X 前）`", text,
                         "不能教她正文寫 #N（那是畫面標記，不是話）")
        self.assertNotIn("#N（X 前）", text)

    def test_clock_block_says_not_to_write_hash(self):
        text = self._clock_system_text()
        self.assertIn("書かない", text, "要明說返答正文不寫 #數字")


class TestSpeechTextStripsMarkers(unittest.TestCase):
    """修法 C：TTS 邊界把 `#N（…）` 去掉，不讓號碼被念出來。"""

    def test_hash_with_parenthetical_removed(self):
        self.assertEqual(speech_text("#112（いま）うん、おやすみ Alice。"), "うん、おやすみ Alice。")

    def test_hash_with_ascii_parenthetical_removed(self):
        self.assertEqual(speech_text("#118(3 分前) ふふ"), "ふふ")

    def test_bare_hash_removed(self):
        self.assertEqual(speech_text("#5"), "")

    def test_plain_text_untouched(self):
        text = "うふふ、どういたしまして。"
        self.assertEqual(speech_text(text), text)

    def test_only_markers_becomes_empty(self):
        self.assertEqual(speech_text("#123（10 分前）"), "")
        self.assertEqual(speech_text("「」"), "")

    def test_none_and_empty(self):
        self.assertEqual(speech_text(None), "")
        self.assertEqual(speech_text(""), "")

    def test_number_without_hash_is_kept(self):
        # 一般的數字要留著念（「20 回前」）
        self.assertEqual(speech_text("それは 20 回前の話ね。"), "それは 20 回前の話ね。")

    def test_fake_sticker_text_removed(self):
        """她在正文裡寫的假貼圖（真的貼圖走 `sticker` 幀）"""
        self.assertEqual(speech_text("（スタンプ：えへへ）"), "")
        self.assertEqual(speech_text("（スタンプ返すね）"), "")
        self.assertEqual(speech_text("うふふ。（スタンプ：えへへ）"), "うふふ。")

    def test_normal_parentheses_are_kept(self):
        """⚠️ 清洗**不能**吃掉正常句子的括號（她寫「これはうん（真面目に）」是合法的）"""
        self.assertEqual(speech_text("これはうん（真面目に）"), "これはうん（真面目に）")
        self.assertEqual(speech_text("ふふ（笑）"), "ふふ（笑）")

    def test_dangling_close_bracket_removed(self):
        """貼圖字串被刪掉後留下的孤立收尾括號要收掉"""
        self.assertEqual(speech_text("終わった瞬間って、いちばん気が抜けるよね。（スタンプを返す"),
                         "終わった瞬間って、いちばん気が抜けるよね。")

    def test_stamp_in_tag_not_echoed(self):
        """主人送貼圖的輸入標籤不該被回抄"""
        self.assertEqual(speech_text("[[stamp_in:うれしい]] ありがとう"), "ありがとう")
        self.assertEqual(speech_text("[[stamp_out:うれしい]] やったね"), "やったね")


class TestCleanKeepsRealWords(unittest.TestCase):
    """複檢 2026-10-29：清洗只能刪標記，正常的話一個字都不能少。"""

    def test_reply_starting_with_a_bracket_survives(self):
        # 以前「只剩括號」那條優先序寫錯 ⇒ 以「「（【」開頭的回覆整句變空字串
        for text in ("「おはよう」って言われた気がする。", "（小声で）うん、いいよ。", "【お知らせ】明日は休み。"):
            self.assertEqual(speech_text(text), text)

    def test_ordinary_hash_numbers_survive(self):
        for text in ("issue #123 は直したよ。", "ランキング#1だった！", "#3（重要）を先にやる。",
                     "#123 は直したよ。"):
            self.assertEqual(speech_text(text), text)

    def test_old_bare_marker_glued_to_text_is_removed(self):
        self.assertEqual(speech_text("#12うん。"), "うん。")

    def test_unclosed_fake_sticker_does_not_eat_the_rest(self):
        self.assertEqual(speech_text("（スタンプ：えへへ それでね、明日も頑張ろう"), "それでね、明日も頑張ろう")
        self.assertEqual(speech_text("（スタンプ：えへへ）それでね（笑）"), "それでね（笑）")


class TestStickerBlockTellsHerSheHasIt(unittest.TestCase):
    """她得知道**自己有能力送貼圖** —— 不然模型不會主動用（實測 0/12、0/20）。

    ⚠️ 只把工具描述寫得鼓勵一點是**沒有用**的（實測 0/12）；
    真正缺的是 prompt 裡對她說明「你有這個手段、什麼時候用」。
    """

    def _sys_texts(self, tools) -> list:
        from agent.brain.ports import BrainRequest
        req = BrainRequest(
            thread_id="th", entity="mira", principal="guest-707", text="やあ",
            system="いま話しているのは Alice。", meta={"tools": tools})
        return [str(m.get("content") or "") for m in build_messages(req, max_history=10)
                if m.get("role") == "system"]

    def _sticker_tool(self):
        return {"type": "function", "function": {
            "name": "sticker_send",
            "description": "スタンプを一枚送る。\n使えるスタンプ：\n- 014_lol：笑った（www）",
            "parameters": {"type": "object",
                           "properties": {"id": {"type": "string"}},
                           "required": ["id"]}}}

    def test_block_present_when_sticker_tool_available(self):
        texts = self._sys_texts([self._sticker_tool()])
        blocks = [x for x in texts if "[[sticker]]" in x]
        self.assertTrue(blocks, "工具清單裡有 sticker_send 時，prompt 必須有 [[sticker]] 段")
        self.assertIn("スタンプ", blocks[0])

    def test_block_absent_when_no_sticker_tool(self):
        texts = self._sys_texts([{"type": "function", "function": {"name": "voice_speak"}}])
        self.assertFalse([x for x in texts if "[[sticker]]" in x],
                         "沒有貼圖工具時不該出現 [[sticker]] 段")

    def test_block_has_trigger_condition(self):
        """要給**具體觸發條件**（對方送貼圖就回一張），不能只說「可以」。"""
        texts = self._sys_texts([self._sticker_tool()])
        block = [x for x in texts if "[[sticker]]" in x][0]
        self.assertIn("[[stamp_in:", block)
        self.assertIn("スタンプで返す", block)
        # 她自己歷史裡的 `[[stamp_out:…]]` 也要說明（不然她不知道那是什麼）
        self.assertIn("[[stamp_out:", block)

    def test_block_is_japanese_only(self):
        """prompt 裡不能用**中文**（三位都是日語母語）。

        ⚠️ 日文漢字與中文共用同一區 Unicode ⇒ 不能用 `[\u4e00-\u9fff]` 判斷
        （會把「表情」「返事」這些日文漢字也報成中文）。
        改用**中文特有**的字（該區塊在日文裡基本不出現）。
        """
        texts = self._sys_texts([self._sticker_tool()])
        block = [x for x in texts if "[[sticker]]" in x][0]
        chinese_only = ("們这为么个样发来对时会说过还进将")
        found = [ch for ch in block if ch in chinese_only]
        self.assertEqual(found, [], "貼圖段裡不能有中文：%r" % found)


if __name__ == "__main__":
    unittest.main()
