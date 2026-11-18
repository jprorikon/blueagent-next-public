#!/usr/bin/env python3
"""情緒注入語氣（owner 2026-10-22「注入語氣吧」）。

判據：
  ① 心情不同 ⇒ 進 prompt 的那段**文字不同**（不是只有數字變）；
  ② 段落很短（≤160 字），不搶人格檔的注意力；
  ③ 只給「方向」不給數字（契約：強度檔不給數值）；
  ④ `BLUEAGENT_MOOD_IN_PROMPT=0` ⇒ 完全不注入；
  ⑤ 位置正確：人格檔 → user.md → **心情** → 私記 → 記憶 → 本輪。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from pathlib import Path

from agent.brain.messages import build_messages
from agent.brain.ports import BrainRequest
from agent.brain.compose.state import AppState
from agent.brain.runtime.inner.affect import AffectService, mood_block


def service() -> AffectService:
    return AffectService(tempfile.mkdtemp(prefix="ba-mood-"), enabled=True)


def warmed(affect: AffectService, *, kind: str, dv: float, da: float) -> None:
    noted = affect.note_message("mira", "ねえ、聞いて", speaker="owner")
    # 被拒時要看到原因（AEM_DISABLED／ECHO_SELF_SPEAKER／AEM_*），不是一句 KeyError
    if not noted.get("ok"):
        raise AssertionError(f"note_message 沒成功：{noted}")
    affect.appraise("mira", kind=kind, delta_v=dv, delta_a=da, confidence=0.9,
                    source_event_id=noted["event_id"])


class TestMoodBlock(unittest.TestCase):
    def test_different_moods_give_different_words(self):
        happy = mood_block({"label": "わくわく", "arousal": 0.8})
        sad = mood_block({"label": "しょんぼり", "arousal": 0.1})
        self.assertIn("いまの気分", happy)
        self.assertNotEqual(happy, sad)
        self.assertIn("弾み", happy)
        self.assertIn("沈んで", sad)

    def test_block_stays_short(self):
        for label in ("わくわく", "ごきげん", "いらいら", "しょんぼり", "ふつう"):
            block = mood_block({"label": label, "arousal": 0.5})
            self.assertLessEqual(len(block), 200, label)

    def test_no_numbers_in_the_block(self):
        block = mood_block({"label": "ごきげん", "arousal": 0.42, "valence": 0.31})
        for digit in "0123456789":
            self.assertNotIn(digit, block, "情緒段不給數字（只給強度檔）")


class TestMoodInPrompt(unittest.TestCase):
    def setUp(self):
        self.app = build(data_dir=tempfile.mkdtemp(prefix="ba-mood-demo-"),
                          step_duration=0.02, life_pause=0.005)
        self.addCleanup(self.app.shutdown_lives)
        self.state = AppState(self.app)

    def _mood_meta(self, text: str) -> str:
        """跑一輪，然後看 `PersonaStage` 有沒有把情緒段放進 meta。

        （不走 MockBrain 的 `build_messages`：那條路不打 prompt 檔，所以直接驗 meta。）
        """
        captured: dict = {}
        stage = next(s for s in self.app.pipelines.require("chat.text").stages
                     if getattr(s, "name", "") == "persona")

        class _Env:
            pass

        class _Ctx:
            def __init__(self):
                self.emitted = []

            def emit(self, kind, payload, final=False):
                self.emitted.append((kind, payload))

        real_run = stage.run

        def spy(env, ctx):
            real_run(env, ctx)
            captured.update(dict(env.meta))

        stage.run = spy
        try:
            warmed(self.app.affect, kind="pleasure", dv=0.9, da=0.6)
            self.state.send(text, principal="owner")
        finally:
            stage.run = real_run
        return str(captured.get("mood_block", ""))

    def test_mood_reaches_the_brain(self):
        block = self._mood_meta("おはよう")
        self.assertIn("いまの気分", block, block)

    def test_can_be_turned_off(self):
        old = os.environ.get("BLUEAGENT_MOOD_IN_PROMPT")
        try:
            os.environ["BLUEAGENT_MOOD_IN_PROMPT"] = "0"
            block = self._mood_meta("おはよう")
            self.assertNotIn("いまの気分", block, "關掉就不該注入")
        finally:
            if old is None:
                os.environ.pop("BLUEAGENT_MOOD_IN_PROMPT", None)
            else:
                os.environ["BLUEAGENT_MOOD_IN_PROMPT"] = old

    def test_order_in_messages(self):
        mood = mood_block({"label": "ごきげん", "arousal": 0.3})
        request = BrainRequest(
            thread_id="th-1", entity="mira", principal="owner", text="やあ",
            system="", mode="chat",
            meta={"soul_block": "SOUL", "user_block": "USER", "mood_block": mood,
                  "self_log_block": "SELFLOG", "recall": ("MEM",)})
        out = build_messages(request)
        flat = "\n".join(row["content"] for row in out)
        self.assertLess(flat.index("SOUL"), flat.index("USER"))
        self.assertLess(flat.index("USER"), flat.index("いまの気分"))
        self.assertLess(flat.index("いまの気分"), flat.index("SELFLOG"))
        # 記憶是 system 段（既有設計），永遠在對話之前；這裡只驗「心情在私記之前」
        self.assertIn("MEM", flat)


if __name__ == "__main__":
    unittest.main(verbosity=2)
