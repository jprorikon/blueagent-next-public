#!/usr/bin/env python3
"""① 她在 Discord 群裡說話後，主人**不 @ 她**但內容是回她 ⇒ 她要收到；
② TRPG 局中她要「以自己情緒為線索」報告場內狀況與感想。

① 的三條路（都不必 @）：
   a. 引用回覆她的訊息（Discord reply，`message.reference` → 作者是 bot）；
   b. 她剛在這條頻道說完（時間窗內）就接著講；
   c. 他 @ 了別人 ⇒ **不算**對她說（不能搶話）。
② 匯報：每輪一次、同一輪不重複、最小間隔內不洗版、走她自己的回合（心情會影響措辭）。
"""

from __future__ import annotations

import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.discord_source import DiscordConfig, DiscordSource
from agent.adapter.trpg import TrpgConfig, TrpgSeat

OWNER_ID = "100000000000000001"
OTHER_ID = "777000111222333444"


def config(**overrides) -> DiscordConfig:
    values = {"DISCORD_BOT_TOKEN": "test-token", "GUILD_ID": "g1",
              "TEXT_CHANNEL_ID": "c1", "ALLOWED_USER_IDS": OWNER_ID}
    values.update(overrides)
    return DiscordConfig.from_env(values)


class TestReplyWithoutMention(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        self.source = DiscordSource(config(), self.state)

    def test_plain_group_message_is_still_listened_only(self):
        """沒 @、沒引用、她也沒剛說完 ⇒ 群裡只是旁聽（不能變成「每句都插話」）。"""
        result = self.source.handle("今日はいい天気だね", OWNER_ID, guild=True,
                                    mentioned=False, is_dm=False, channel_id="c1")
        self.assertEqual(result.get("engage"), "silent", f"應只旁聽：{result}")
        self.assertFalse(result.get("text"), "不該插話")

    def test_reply_to_her_message_gets_an_answer(self):
        """引用回覆她 → 算在對她說（他回她卻沒收到，就是這條缺了）。"""
        result = self.source.handle("それで、どう思う？", OWNER_ID, guild=True,
                                    mentioned=False, is_dm=False, channel_id="c1",
                                    reply_to_me=True)
        self.assertTrue(result["ok"])
        self.assertTrue(str(result.get("text") or "").strip(), "引用回覆她要回")
        self.assertEqual(result.get("engage"), "reply", f"判定應為 reply：{result}")

    def test_continuation_after_she_spoke_gets_an_answer(self):
        """她剛說完，主人接著講（不 @）⇒ 算延續對話。"""
        self.source._note_spoke("c1")
        result = self.source.handle("なるほどね", OWNER_ID, guild=True, mentioned=False,
                                    is_dm=False, channel_id="c1", continuation=True)
        self.assertTrue(str(result.get("text") or "").strip(), "接著講要回")

    def test_continuation_window_is_configurable(self):
        self.assertEqual(config().followup_window_s, 180.0)
        self.assertEqual(config(BLUEAGENT_DISCORD_FOLLOWUP_S="0").followup_window_s, 0.0)

    def test_mentioning_someone_else_is_not_for_her(self):
        """他 @ 別人 ⇒ 不是對她說（addressed_to 保持空 → 旁聽）。"""
        result = self.source.handle("これは他の人への話", OWNER_ID, guild=True,
                                    mentioned=False, is_dm=False, channel_id="c1",
                                    continuation=False, reply_to_me=False)
        self.assertFalse(result.get("text"))

    def test_mention_still_works(self):
        result = self.source.handle("ねえ聞いてる？", OWNER_ID, guild=True, mentioned=True,
                                    is_dm=False, channel_id="c1")
        self.assertTrue(str(result.get("text") or "").strip())


class FakeTable:
    def __init__(self, view: dict) -> None:
        self.view_data = view
        self.said: list[str] = []
        self.acted: list[tuple] = []

    def view(self, seat=None):
        return dict(self.view_data)

    def say(self, text):
        self.said.append(text)
        return {"ok": True}

    def act(self, action, *, target=None, note=None, skill=None, item=None):
        self.acted.append((action, target))
        return {"ok": True}


def view(round_no: int = 1) -> dict:
    return {"table": {"roundNo": round_no}, "round_no": round_no,
            "you": {"seat": "p1", "name": "ミラ", "hp": 8, "max_hp": 10,
                    "memory": 4, "max_memory": 6, "star_coins": 2},
            "turn": {"seat": "p1"},
            "board": {"you_are": {"id": "n_forest", "cn": "森"},
                      "can_go": [{"to": "flavio", "cn": "フラヴィオ"}]},
            "allowed_actions": ["move", "fight", "end_turn"],
            "log": []}


class TestTrpgReport(unittest.TestCase):
    def make(self, *, rounds_gap: int = 1, min_gap: float = 0.0, replies=None):
        self.table = FakeTable(view(2))
        self.reports: list[tuple[str, dict]] = []
        queue = list(replies or ["星骸を斬る！", "ちょっと怖かったよ、でも進む。"])

        def speak(prompt: str, purpose: str = "trpg") -> str:
            self.prompts.append(purpose)
            return queue.pop(0) if queue else ""

        self.prompts: list[str] = []
        config_ = TrpgConfig(enabled=True, table_id="t1", session="s1", seat="p1")
        self.seat = TrpgSeat(config_, self.table, speak,
                             report=lambda text, meta: self.reports.append((text, meta)) or True,
                             report_every_rounds=rounds_gap, report_min_gap_s=min_gap)

    def test_reports_once_per_round(self):
        self.make()
        self.seat.step({})
        self.assertEqual(len(self.reports), 1, f"要匯報一次：{self.reports}")
        self.assertIn("trpg-report", self.prompts, "匯報要另外走一次她的回合")
        self.assertIn("怖かった", self.reports[0][0])
        self.assertEqual(self.reports[0][1]["round"], 2)

    def test_same_round_is_not_reported_twice(self):
        self.make()
        self.seat.step({})
        self.seat.step({})                     # 同一輪再被叫一次
        self.assertEqual(len(self.reports), 1, "同一輪不重複匯報")

    def test_min_gap_prevents_flooding(self):
        self.make(min_gap=600.0)
        self.seat.step({})
        self.table.view_data = view(3)
        self.seat.step({})
        self.assertEqual(len(self.reports), 1, "最小間隔內不該再報")

    def test_every_n_rounds(self):
        self.make(rounds_gap=2,
                  replies=["斬る！", "怖いけど進む", "まだ行ける", "ここは落ち着こう"])
        self.table.view_data = view(3)          # 3 % 2 ≠ 0 ⇒ 不報
        self.seat.step({})
        self.assertEqual(self.reports, [])
        self.table.view_data = view(4)          # 4 % 2 == 0 ⇒ 報
        self.seat.step({})
        self.assertEqual(len(self.reports), 1)

    def test_report_failure_does_not_break_the_hand(self):
        self.make()
        self.seat.report = lambda text, meta: False
        result = self.seat.step({})
        self.assertTrue(result["ok"], "匯報送不出去也不能影響這一手")
        self.assertEqual(self.seat.reports, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
