#!/usr/bin/env python3
"""「附帶語音重複兩遍文字內容」——串流已唸過的段落又被整段合成一次。

實測取證（owner 2026-10-22）：把 Discord 上那則 `reply.wav`（29.92 s）丟進容器 STT 轉寫，
文字原封不動出現**兩次**：
```
目ざといねオーナー 論文の著者名を…相性がいい遊びだしね
目ざといねオーナー 論文の著者名を…相性がいい遊びだしね！
```
根因：`spoken` 在累積時被 `.strip()` 過，而回覆含空行 ⇒ `reply.startswith(spoken)` 失敗
⇒ `VoiceStage` 認為「一句都還沒唸」，把整段又合成一遍。

判據：
  ① 去掉空白後比對邊界（含空行也接得上）；
  ② 整段已唸 ⇒ 回空字串（不再合成）；
  ③ 剩尾巴 ⇒ 只回尾巴，且保留原文換行；
  ④ 完全沒唸過 ⇒ 原樣回全部。
"""

from __future__ import annotations

import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.stages import _remaining_after_spoken, _squash


class TestRemaining(unittest.TestCase):
    def test_nothing_spoken_returns_everything(self):
        self.assertEqual(_remaining_after_spoken("おはよう。元気？", ""), "おはよう。元気？")

    def test_whole_reply_already_spoken_returns_empty(self):
        self.assertEqual(_remaining_after_spoken("おはよう。元気？", "おはよう。元気？"), "")
        self.assertEqual(_remaining_after_spoken("おはよう。", "おはよう。"), "")

    def test_blank_lines_between_sentences_still_match(self):
        # 這是實際案例：回覆含空行，口語端只唸過第一句（原文無空行差異）
        self.assertEqual(_remaining_after_spoken("あ。\n\nい。", "あ。"), "い。")

    def test_tail_keeps_original_newlines(self):
        reply = "ふふ、目ざといねオーナー。\n\n論文の話、続ける？"
        rest = _remaining_after_spoken(reply, "ふふ、目ざといねオーナー。\n\n")
        self.assertEqual(rest, "論文の話、続ける？")

    def test_partial_mismatch_returns_full_text(self):
        # 唸過的內容跟回覆接不上（例如回覆被工具改寫）⇒ 保守起見整段重唸，不吞字
        self.assertEqual(_remaining_after_spoken("全然違う話", "おはよう。"), "全然違う話")

    def test_whitespace_only_difference_is_ignored(self):
        self.assertEqual(_squash("あ い\nう"), "あいう")
        self.assertEqual(_remaining_after_spoken("あ い\nう", "あいう"), "")


class TestVoiceStageDoesNotRepeat(unittest.TestCase):
    def test_streamed_reply_is_not_synthesised_again(self):
        """整段已經邊生成邊唸過 ⇒ 收尾那個 stage 不該再合成一次。"""
        state = make_state()
        self.addCleanup(state.shutdown)
        result = state.send("おはよう、今日は何する？", entity="mira", principal="owner")
        meta = result.get("meta") or {}
        spoken = str(meta.get("spoken_text") or "")
        reply = state.reply_text(result)
        if not spoken:
            self.skipTest("這一輪沒有走串流發聲（mock 沒 TTS）")
        frames = [frame for frame in (result.get("frames") or [])
                  if frame.get("kind") == "speech"]
        self.assertLessEqual(len(frames), max(1, reply.count("。") + 1),
                             f"speech 幀數不該超過句子數：spoken={spoken!r} reply={reply!r}")
        self.assertEqual(_remaining_after_spoken(reply, spoken), "",
                         f"整段都唸過了，不該剩東西再唸：spoken={spoken!r} reply={reply!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
