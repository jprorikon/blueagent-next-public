#!/usr/bin/env python3
"""「对话混乱」三刀：VC 碎片合并／短回覆與後續重複／匯報要看情緒門檻。

症狀（owner 2026-10-22）：语音里他把一句话讲完，切句器切成「。」「しま。」「で。」「The.」
好几段，每段各起一轮 ⇒ 她连珠炮回问；文字频道里则是短回覆与完整答案问同一句。
"""

from __future__ import annotations

import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.discord_voice import DiscordVoiceSession
from agent.adapter.trpg import TrpgConfig, TrpgSeat
from agent.brain.compose.state import _same_gist


class TestFragmentMerge(unittest.TestCase):
    def session(self, *, gap: float = 0.15):
        import os
        os.environ["BLUEAGENT_VC_MERGE_GAP_S"] = str(gap)
        voice = DiscordVoiceSession.__new__(DiscordVoiceSession)   # 不跑 __init__（會碰 discord）
        voice.merge_gap_s = gap
        voice._pending = {}
        voice._pending_at = {}
        voice._pending_lock = __import__("threading").RLock()
        voice.merged = 0
        voice.noise_dropped = 0
        voice._log = lambda _message: None
        voice.sent = []
        voice._respond = lambda user_id, text: voice.sent.append((user_id, text))
        return voice

    def test_fragments_within_window_are_merged(self):
        voice = self.session()
        for piece in ("さっきのゲーム", "とったった", "。"):
            voice._buffer_fragment("u1", piece)
        import time
        time.sleep(0.5)
        self.assertEqual(len(voice.sent), 1, f"應該併成一句：{voice.sent}")
        self.assertIn("さっきのゲーム", voice.sent[0][1])
        self.assertIn("とったった", voice.sent[0][1])

    def test_separate_utterances_stay_separate(self):
        voice = self.session(gap=0.1)
        voice._buffer_fragment("u1", "おはよう")
        import time
        time.sleep(0.35)
        voice._buffer_fragment("u1", "今日は何する")
        time.sleep(0.35)
        self.assertEqual(len(voice.sent), 2, f"兩句要分開：{voice.sent}")

    def test_different_speakers_do_not_merge(self):
        voice = self.session()
        voice._buffer_fragment("u1", "おはよう")
        voice._buffer_fragment("u2", "こんばんは")
        import time
        time.sleep(0.4)
        speakers = [row[0] for row in voice.sent]
        self.assertEqual(sorted(speakers), ["u1", "u2"])


class TestNoiseFilter(unittest.TestCase):
    def test_punctuation_and_latin_noise_is_dropped(self):
        self.assertTrue(DiscordVoiceSession._is_noise("。"))
        self.assertTrue(DiscordVoiceSession._is_noise("The."))
        self.assertTrue(DiscordVoiceSession._is_noise("  "))
        self.assertFalse(DiscordVoiceSession._is_noise("しま。"))
        self.assertFalse(DiscordVoiceSession._is_noise("今日はゲームを取った"))


class TestFollowupDedupe(unittest.TestCase):
    def test_same_gist_detects_near_duplicates(self):
        self.assertTrue(_same_gist("ん、役どころは誰にする？", "ん、役どころは誰にする？"))
        self.assertTrue(_same_gist("どの子のカードにする？", "うふふ、どの子のカードにする？"))
        self.assertFalse(_same_gist("どの子のカードにする？",
                                    "判定は三つ決めたよ。職業だけ決まってないんだ。"))

    def test_duplicate_followup_is_not_delivered(self):
        state = make_state()
        self.addCleanup(state.shutdown)
        delivered: list[dict] = []
        # 直接驅動「短回覆 → 後續」那條路：ack 與 followup 講同一句
        state.send_voice("カードの話", entity="mira", principal="owner", mode="work",
                         on_followup=lambda follow: delivered.append(follow),
                         ack_first=True)
        # 等背景那支後續執行緒真的跑完（以前是乾等滿 5 秒：斷言的是「沒送出」，等不到東西就只能等到逾時）
        import threading
        for worker in [t for t in threading.enumerate() if t.name.startswith("ba-followup-")]:
            worker.join(timeout=5)
        # mock 大腦兩次都回同一句 ⇒ 應該被吃掉
        self.assertEqual(delivered, [], f"重複的後續不該送出：{delivered}")


class TestReportIntensityGate(unittest.TestCase):
    def table(self):
        class Table:
            def view(self, seat=None):
                return {"table": {"roundNo": 3}, "round_no": 3,
                        "you": {"seat": "p1", "name": "ミラ"}, "turn": {"seat": "p1"},
                        "board": {"you_are": {"id": "n"}, "can_go": [{"to": "x", "cn": "X"}]},
                        "allowed_actions": ["move", "end_turn"], "log": []}

            def say(self, text):
                return {"ok": True}

            def act(self, action, **kwargs):
                return {"ok": True}

        return Table()

    def seat(self, level: float, threshold: float = 0.6):
        reports: list[str] = []
        queue = ["行くよ", "ちょっと怖かった"]

        def speak(prompt, purpose="trpg"):
            return queue.pop(0) if queue else ""

        config = TrpgConfig(enabled=True, table_id="t", session="s", seat="p1")
        who = TrpgSeat(config, self.table(), speak,
                       report=lambda text, meta: reports.append(text) or True,
                       report_min_gap_s=0.0,
                       intensity=lambda: level, min_intensity=threshold)
        return who, reports

    def test_below_threshold_is_silent(self):
        who, reports = self.seat(level=0.3)
        who.step({})
        self.assertEqual(reports, [], "情緒不夠就不報")
        self.assertAlmostEqual(who.last_intensity, 0.3, places=3)

    def test_above_threshold_reports(self):
        who, reports = self.seat(level=0.8)
        who.step({})
        self.assertEqual(len(reports), 1, f"情緒超過門檻要報：{reports}")

    def test_threshold_is_configurable(self):
        who, reports = self.seat(level=0.5, threshold=0.4)
        who.step({})
        self.assertEqual(len(reports), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
