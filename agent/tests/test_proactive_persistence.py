#!/usr/bin/env python3
"""主動消息的四個改動（owner 2026-10-22「abcd 都做」）。

判據：
  A 閘門狀態落盤：重啟後「20 分鐘間隔／今日次數」不清零（原本重啟就送一句）；
  B 啟動暖機：剛起來的 N 秒內一次都不准；
  C 公開的主动消息**先貼群**，私事才 DM（原本先 DM ⇒ 每次重啟都戳私聊）；
  D 台詞由她自己組織：心跳先過閘再合（不會白叫模型），合不出來退回模板。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.discord_source import DiscordConfig, DiscordSource
from agent.brain.runtime.delivery.delivery import DiscordChannel
from agent.brain.runtime.heartbeat import Heartbeat
from agent.brain.runtime.proactive import ProactiveGate

OWNER_ID = "100000000000000001"


class TestGatePersistsAcrossRestart(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.path = self.tmp / "proactive.json"

    def gate(self, **kwargs) -> ProactiveGate:
        settings = dict(min_gap_s=1200, daily_max=3, quiet_start=0, quiet_end=0,
                        state_path=self.path, warmup_s=0)
        settings.update(kwargs)
        return ProactiveGate(**settings)

    def test_gap_survives_restart(self):
        first = self.gate()
        self.assertTrue(first.allows("mira", now=10_000.0)[0])
        first.mark("mira", now=10_000.0)
        again = self.gate()                       # 模擬重啟
        ok, reason = again.allows("mira", now=10_100.0)
        self.assertFalse(ok, "重啟不該讓 20 分鐘間隔清零")
        self.assertEqual(reason, "min_gap")
        self.assertTrue(again.allows("mira", now=10_000.0 + 1300)[0])

    def test_daily_count_survives_restart(self):
        first = self.gate(daily_max=1)
        first.mark("mira", now=5_000.0)
        again = self.gate(daily_max=1)
        ok, reason = again.allows("mira", now=5_000.0 + 5000)
        self.assertFalse(ok)
        self.assertEqual(reason, "daily_max")

    def test_warmup_blocks_everything_at_start(self):
        gate = ProactiveGate(min_gap_s=0, daily_max=9, quiet_start=0, quiet_end=0,
                             warmup_s=600, clock=lambda: 100_000.0)
        ok, reason = gate.allows("mira")
        self.assertFalse(ok)
        self.assertEqual(reason, "warmup")

    def test_state_file_is_written(self):
        gate = self.gate()
        gate.mark("mira", now=7_000.0)
        self.assertTrue(self.path.is_file(), "要落盤，不然重啟一定失憶")
        self.assertIn("mira", self.path.read_text(encoding="utf-8"))


class TestHeartbeatComposesOnlyWhenAllowed(unittest.TestCase):
    def setUp(self):
        self.calls: list[str] = []

        class Affect:
            def advance(self, entity):
                return None

            def mood(self, entity, *, at=None):
                return {"label": "ふつう", "propensity": 0.9, "social_need": 0.1,
                        "valence": 0.4, "arousal": 0.4}

        self.affect = Affect()

    def beat(self, gate, compose) -> Heartbeat:
        return Heartbeat(entity="mira", affect=self.affect, gate=gate,
                         compose=compose, threshold=0.35,
                         log=lambda _m: None)

    def test_blocked_gate_does_not_call_the_model(self):
        gate = ProactiveGate(min_gap_s=600, daily_max=9, quiet_start=0, quiet_end=0,
                             state_path=pathlib.Path(tempfile.mkdtemp()) / "p.json",
                             warmup_s=0)
        gate.mark("mira", now=1000.0)
        beat = self.beat(gate, lambda entity, mood: self.calls.append("compose") or "やあ")
        beat.tick(at=1010.0)                      # 10 秒後 ⇒ 被 min_gap 擋
        self.assertEqual(self.calls, [], "被擋掉就不該花一次模型呼叫")
        self.assertEqual(beat.last_skip_reason, "min_gap")

    def test_composer_text_is_used(self):
        gate = ProactiveGate(min_gap_s=0, daily_max=9, quiet_start=0, quiet_end=0,
                             state_path=pathlib.Path(tempfile.mkdtemp()) / "p.json",
                             warmup_s=0)
        sent: list[dict] = []
        beat = Heartbeat(entity="mira", affect=self.affect, gate=gate,
                         publish=lambda principal, frame: sent.append(frame) or True,
                         compose=lambda entity, mood: "ねえ、ちょっといい？",
                         threshold=0.35, log=lambda _m: None)
        beat.tick(at=2000.0)
        self.assertEqual([frame["text"] for frame in sent], ["ねえ、ちょっといい？"])

    def test_composer_failure_falls_back_to_template(self):
        gate = ProactiveGate(min_gap_s=0, daily_max=9, quiet_start=0, quiet_end=0,
                             state_path=pathlib.Path(tempfile.mkdtemp()) / "p.json",
                             warmup_s=0)
        sent: list[dict] = []
        beat = Heartbeat(entity="mira", affect=self.affect, gate=gate,
                         publish=lambda principal, frame: sent.append(frame) or True,
                         compose=lambda entity, mood: "",        # 合不出來
                         threshold=0.35, log=lambda _m: None)
        beat.tick(at=3000.0)
        self.assertTrue(sent, "合不出來也要能說話（退回模板）")
        self.assertIn("　", sent[0]["text"], "模板形狀＝一句頭 ＋ 一句身")


class TestProactiveGoesToGroupFirst(unittest.TestCase):
    def make(self, *, channel: str = "100000000000000003") -> tuple:
        posts: list[tuple] = []

        class FakeSource:
            config = DiscordConfig.from_env({"DISCORD_BOT_TOKEN": "t",
                                             "ALLOWED_USER_IDS": OWNER_ID,
                                             "TEXT_CHANNEL_ID": channel})

            def can_dm(self, principal):
                return True

            def send_dm(self, principal, text):
                posts.append(("dm", text))
                return True

            def send_text(self, text):
                posts.append(("group", text))
                return True

        return DiscordChannel(FakeSource()), posts

    def test_public_first_goes_to_group(self):
        channel, posts = self.make()
        self.assertTrue(channel.send("owner", {"text": "あのね", "visibility": "public"}))
        self.assertEqual(posts[0][0], "group", "公開的事先貼群，不要戳私聊")

    def test_private_stays_in_dm(self):
        channel, posts = self.make()
        self.assertTrue(channel.send("owner", {"text": "ないしょ", "visibility": "private"}))
        self.assertEqual([kind for kind, _ in posts], ["dm"])

    def test_group_failure_falls_back_to_dm(self):
        posts: list[tuple] = []

        class FakeSource:
            config = DiscordConfig.from_env({"DISCORD_BOT_TOKEN": "t",
                                             "ALLOWED_USER_IDS": OWNER_ID,
                                             "TEXT_CHANNEL_ID": "1"})

            def can_dm(self, principal):
                return True

            def send_dm(self, principal, text):
                posts.append(("dm", text))
                return True

            def send_text(self, text):
                posts.append(("group", text))
                return False

        channel = DiscordChannel(FakeSource())
        self.assertTrue(channel.send("owner", {"text": "やあ"}))
        self.assertEqual([kind for kind, _ in posts], ["group", "dm"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
