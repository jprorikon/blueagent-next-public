#!/usr/bin/env python3
"""她在語音房的在場決策（owner 2026-10-22：給她主動進出的權）。

判據：
  ① 房裡沒人（除了她）持續 `empty_leave_s` ⇒ 她自己退；剛進房的前幾秒不算；
  ② 她不在房裡、主人進房 ⇒ **先問**一句（不是擅自進去）；同一個人冷卻期內不重問；安靜時段不問；
  ③ 白名單外的人進房 ⇒ 她什麼都不做（也不把他算成「有人」，所以他走了照樣退）；
  ④ 主人一句「进来／退吧」⇒ 真的進／退（確定性命令，不經模型），敘述句不會誤判；
  ⑤ 決策只回動作，不碰網路（連線／貼文字在 adapter）。
"""

from __future__ import annotations

import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.discord_voice import DiscordVoiceSession, VoiceConfig, build_voice
from agent.brain.runtime.voice.vc_presence import (ASK, JOIN, LEAVE, PresenceConfig, VCPresence,
                                                parse_voice_command)

OWNER = "100000000000000001"
STRANGER = "999999999999999999"
ROOM = "100000000000000004"
OTHER_ROOM = "111"


def presence(**overrides) -> VCPresence:
    settings = dict(channel_id=ROOM, empty_leave_s=120.0, ask_cooldown_s=900.0,
                    join_grace_s=15.0, autojoin=False)
    settings.update(overrides)
    return VCPresence(PresenceConfig(**settings), allowed={OWNER})


class TestParseVoiceCommand(unittest.TestCase):
    def test_join_words(self):
        for text in ("进来", "进来吧", "进来呀", "入って", "来て", " 进来！", "入って。"):
            self.assertEqual(parse_voice_command(text), JOIN, text)

    def test_leave_words(self):
        for text in ("退吧", "出去", "退下", "出て", "退室"):
            self.assertEqual(parse_voice_command(text), LEAVE, text)

    def test_sentences_are_not_commands(self):
        for text in ("我要进去了", "你刚才怎么进去了", "我先进去一下", "", "   ",
                     "进来这个词怎么说", "退吧退吧退吧退吧退吧"):
            self.assertEqual(parse_voice_command(text), "", text)


class TestLeaveWhenAlone(unittest.TestCase):
    def test_leaves_after_empty_window(self):
        state = presence()
        state.note_joined(now=100.0)
        state.sync_members([OWNER], now=100.0)
        self.assertEqual(state.tick(now=110.0), [], "房裡有人 ⇒ 不退")
        state.observe(user_id=OWNER, is_bot=False, before_channel=ROOM,
                      after_channel="", now=200.0)          # 主人離開
        self.assertEqual(state.tick(now=250.0), [], "還沒到 120 秒")
        actions = state.tick(now=321.0)
        self.assertEqual([a["action"] for a in actions], [LEAVE])
        self.assertEqual(actions[0]["reason"], "empty_room")
        self.assertEqual(state.leaves, 1)

    def test_grace_after_joining(self):
        state = presence(empty_leave_s=5.0, join_grace_s=15.0)
        state.note_joined(now=0.0)
        self.assertEqual(state.tick(now=10.0), [], "剛進房的前 15 秒不判空")
        self.assertEqual([a["action"] for a in state.tick(now=30.0)], [LEAVE])

    def test_strangers_do_not_count_as_company(self):
        state = presence(empty_leave_s=10.0, join_grace_s=0.0)
        state.note_joined(now=0.0)
        state.sync_members([STRANGER], now=0.0)
        self.assertEqual(state.humans, set())
        self.assertEqual(state.others, {STRANGER})
        self.assertEqual([a["action"] for a in state.tick(now=20.0)], [LEAVE],
                         "只有陌生人也在 ⇒ 一樣是「沒人」")

    def test_switch_off_never_leaves(self):
        state = presence(empty_leave_s=0)
        state.note_joined(now=0.0)
        self.assertEqual(state.tick(now=10_000.0), [])


class TestAskBeforeJoining(unittest.TestCase):
    def test_asks_when_owner_enters_and_she_is_out(self):
        state = presence()
        actions = state.observe(user_id=OWNER, is_bot=False, before_channel="",
                                after_channel=ROOM, now=100.0)
        self.assertEqual([a["action"] for a in actions], [ASK])
        self.assertEqual(actions[0]["user_id"], OWNER)
        self.assertEqual(state.asks, 1)

    def test_cooldown_prevents_repeat(self):
        state = presence(ask_cooldown_s=600)
        state.observe(user_id=OWNER, is_bot=False, before_channel="",
                      after_channel=ROOM, now=100.0)
        again = state.observe(user_id=OWNER, is_bot=False, before_channel="",
                              after_channel=ROOM, now=200.0)
        self.assertEqual(again, [], "同一個人冷卻期內不再問")
        later = state.observe(user_id=OWNER, is_bot=False, before_channel="",
                              after_channel=ROOM, now=800.0)
        self.assertEqual([a["action"] for a in later], [ASK])

    def test_quiet_hours_do_not_ask(self):
        state = presence()
        actions = state.observe(user_id=OWNER, is_bot=False, before_channel="",
                                after_channel=ROOM, quiet=True, now=100.0)
        self.assertEqual(actions, [], "安靜時段不主動問")

    def test_stranger_entering_is_ignored(self):
        state = presence()
        self.assertEqual(state.observe(user_id=STRANGER, is_bot=False, before_channel="",
                                       after_channel=ROOM, now=100.0), [])

    def test_autojoin_goes_in_directly(self):
        state = presence(autojoin=True)
        actions = state.observe(user_id=OWNER, is_bot=False, before_channel="",
                                after_channel=ROOM, now=100.0)
        self.assertEqual([a["action"] for a in actions], [JOIN])

    def test_already_inside_means_no_ask(self):
        state = presence()
        state.note_joined(now=50.0)
        self.assertEqual(state.observe(user_id=OWNER, is_bot=False, before_channel="",
                                       after_channel=ROOM, now=100.0), [])

    def test_other_rooms_are_not_her_business(self):
        state = presence()
        self.assertEqual(state.observe(user_id=OWNER, is_bot=False, before_channel="",
                                       after_channel=OTHER_ROOM, now=100.0), [])


class TestAdapterWiring(unittest.TestCase):
    def setUp(self):
        self.calls: list[tuple] = []
        self.config = VoiceConfig(enabled=True, channel_id=ROOM)
        session = DiscordVoiceSession(self.config, None,
                                      log=lambda _m: None,
                                      presence_config=PresenceConfig(channel_id=ROOM,
                                                                     empty_leave_s=60))
        session.presence.allowed = {OWNER}
        session.request_join = lambda reason="": self.calls.append(("join", reason)) or True
        session.request_leave = lambda reason="": self.calls.append(("leave", reason)) or True
        session._ask_join = lambda user: self.calls.append(("ask", user))
        self.session = session

    def test_observe_routes_actions_to_the_real_actions(self):
        self.session.observe_voice_state(user_id=OWNER, is_bot=False,
                                         before_channel="", after_channel=ROOM)
        self.assertEqual(self.calls, [("ask", OWNER)])

    def test_tick_leaves_when_empty(self):
        self.session.presence.note_joined(now=0.0)
        self.session.presence.empty_since = 0.0
        for action in self.session.presence.tick(now=1000.0):
            self.session._run_action(action)
        self.assertEqual(self.calls[-1][0], "leave")

    def test_health_exposes_presence(self):
        health = self.session.health()
        self.assertIn("presence", health)
        self.assertEqual(health["presence"]["autojoin"], False)
        self.assertIn("empty_leave_s", health["presence"])

    def test_build_voice_reads_presence_env(self):
        session = build_voice({"BLUEAGENT_DISCORD_VC": "1", "VOICE_CHANNEL_ID": ROOM,
                               "BLUEAGENT_VC_EMPTY_LEAVE_S": "30",
                               "BLUEAGENT_VC_AUTOJOIN": "1"}, None)
        self.assertEqual(session.presence.config.empty_leave_s, 30.0)
        self.assertTrue(session.presence.config.autojoin)


if __name__ == "__main__":
    unittest.main(verbosity=2)
