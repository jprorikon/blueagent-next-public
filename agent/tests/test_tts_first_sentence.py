#!/usr/bin/env python3
"""首句優先：邊生成邊合成，第一句一完整就先出聲（現役橋的做法，owner 2026-10-22 追問）。

判據：
  ① 第一個 `speech` 幀必須**在 `done` 之前**送出（＝不用等整段生成完）；
  ② 每句話只唸一次（不會因為 egress 又從頭唸一遍）；
  ③ 短碎片（「え。」）不單獨合成——併到下一句，避免一輪冒出七八個氣音；
  ④ `BLUEAGENT_TTS_STREAM=0` ⇒ 回到舊行為（整段生成完才合成）。
"""

from __future__ import annotations

import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.stages import _take_sentences


class TestSentenceSplitter(unittest.TestCase):
    def test_splits_on_japanese_punctuation(self):
        pieces = _take_sentences("おはよう。今日はいい天気だね！")
        self.assertEqual([p.strip() for p in pieces], ["おはよう。", "今日はいい天気だね！"])

    def test_keeps_incomplete_tail(self):
        pieces = _take_sentences("おはよう。まだ書きかけ")
        self.assertEqual([p.strip() for p in pieces], ["おはよう。"])

    def test_short_fragments_are_merged(self):
        # 「え。」只有 2 字 ⇒ 不切，等下一句一起
        pieces = _take_sentences("え。それは困ったね。")
        self.assertEqual([p.strip() for p in pieces], ["え。それは困ったね。"])


class TestFirstSentenceStreaming(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ba-stream-")
        self.app = build(brain_delay=0, data_dir=self.root,
                          step_duration=0.02, life_pause=0.005)
        self.state = AppState(self.app)
        self.addCleanup(self.app.shutdown_lives)

    def _frames(self, result):
        return [frame.get("kind") for frame in (result["decision"].get("frames") or [])]

    def test_speech_starts_before_done(self):
        result = self.state.send("用语音说话", principal="owner", want_voice=True)
        kinds = self._frames(result)
        self.assertIn("speech", kinds, f"應該有語音幀：{kinds}")
        self.assertLess(kinds.index("speech"), kinds.index("done"),
                        "第一個語音幀必須在 done 之前（首句優先）")

    def test_each_sentence_is_spoken_once(self):
        result = self.state.send("用语音说话", principal="owner", want_voice=True)
        frames = [f for f in (result["decision"].get("frames") or [])
                  if f.get("kind") == "speech"]
        spoken = [str(f["payload"].get("text") or "") for f in frames]
        self.assertTrue(spoken)
        self.assertEqual(len(spoken), len(set(spoken)), f"有句子被唸了兩次：{spoken}")

    def test_stream_can_be_disabled(self):
        """關掉首句優先 ⇒ 回到舊行為：語音在 `done` **之後**才出（整段生成完才合成）。"""
        import os
        os.environ["BLUEAGENT_TTS_STREAM"] = "0"
        try:
            result = self.state.send("用语音说话", principal="owner", want_voice=True)
            kinds = self._frames(result)
            self.assertIn("speech", kinds, kinds)
            self.assertGreater(kinds.index("speech"), kinds.index("done"),
                               "關掉之後語音應該在 done 之後（不是首句優先）")
        finally:
            os.environ.pop("BLUEAGENT_TTS_STREAM", None)

    def test_no_voice_request_means_no_speech(self):
        # ⚠️ 句子裡不能出現「用语音」——MockBrain 的 `_SPEAK` 會因此去呼叫 voice.speak 工具，
        # 那是「工具要求出聲」，不是這條測的「沒要求就不出聲」。
        result = self.state.send("今天过得怎么样", principal="owner", want_voice=False)
        self.assertNotIn("speech", self._frames(result))


if __name__ == "__main__":
    unittest.main(verbosity=2)
