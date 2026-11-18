#!/usr/bin/env python3
"""共同基盤（docs/PLAN-common-ground-20261109.md）第 2／3 步：她知道的 ≠ 對方知道的。

判據：
  ① `[[memory]]` 不再替對方擔保（舊標題「あなたたちが前に本当に知っていること」）；
     按「這一場的人也知道嗎」分兩段，對方沒聽過的那段明寫「話すなら前提から」。
  ② 判定是規則（層／轉述／生活流），不調模型；不知道來歷的保守當「只有她知道」。
  ③ 提前想不再預排「對方開口後我先說什麼」（話題碰撞）；`[[notes]]` 明寫對方的話先。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.stages.recall import _Recalled, shared_with_room
from agent.brain.memory.facts import STREAM_LIFE, Fact, LIFE_SOURCES
from agent.brain.messages import build_messages
from agent.brain.ports import BrainRequest

OLD_HEADER = "あなたたちが前に本当に知っていること"


def memory_text(meta: dict) -> str:
    request = BrainRequest(thread_id="th_x", entity="mira", principal="owner",
                           text="ねえ", system="persona", meta=meta)
    for item in build_messages(request):
        content = str(item.get("content") or "")
        if "[[memory]]" in content:
            return content
    return ""


class TestSharedWithRoom(unittest.TestCase):
    def test_room_layers_are_shared(self) -> None:
        for layer in ("pair", "private", "circle", "consensus"):
            fact = Fact(text="主人は寒いのが苦手", layer=layer, audience=frozenset({"owner", "mira"}))
            self.assertTrue(shared_with_room(fact), layer)
        self.assertTrue(shared_with_room(_Recalled("（以前のやりとり）表の話", layer="window")))

    def test_her_own_knowledge_is_not_shared(self) -> None:
        self.assertFalse(shared_with_room(Fact(text="観測表を作った", layer="self")))
        self.assertFalse(shared_with_room(Fact(text="雨の日は気圧が下がる", layer="world")))
        self.assertFalse(shared_with_room(_Recalled("何か", layer="")), "層不明 ⇒ 保守")

    def test_hearsay_is_not_shared(self) -> None:
        fact = Fact(text="主人は猫が好き", layer="pair", audience=frozenset({"owner", "aster"}),
                    hearsay_from="アスター")
        self.assertFalse(shared_with_room(fact), "聽別人說的 ⇒ 對方沒在她面前說過")

    def test_life_stream_is_not_shared(self) -> None:
        fact = Fact(text="資料を三本読んだ", layer="pair", stream=STREAM_LIFE,
                    audience=frozenset({"owner", "mira"}))
        self.assertFalse(shared_with_room(fact))
        if LIFE_SOURCES:
            source = sorted(LIFE_SOURCES)[0]
            legacy = Fact(text="資料を読んだ", layer="pair", source=source,
                          audience=frozenset({"owner", "mira"}))
            self.assertFalse(shared_with_room(legacy), "舊資料沒填 stream ⇒ 看 source")


class TestMemoryBlock(unittest.TestCase):
    META = {"recall": ["主人は寒いのが苦手", "〔私記〕観測表を作った"],
            "recall_shared": [True, False]}

    def test_split_into_two_sections(self) -> None:
        text = memory_text(dict(self.META))
        self.assertNotIn(OLD_HEADER, text, "不再替對方擔保")
        self.assertIn("### 相手も知っていること", text)
        self.assertIn("### あなただけが知っていること", text)
        self.assertLess(text.index("主人は寒いのが苦手"), text.index("### あなただけ"))
        self.assertGreater(text.index("観測表を作った"), text.index("### あなただけ"))
        self.assertIn("1. 主人は寒いのが苦手", text)
        self.assertIn("2. 〔私記〕観測表を作った", text, "編號連續")
        self.assertIn("〔私記〕が付いているのは", text, "線路 A 的說明照舊")
        self.assertIn("「あれ」「例の」「前に言った」で指さない", text)

    def test_group_names_the_room(self) -> None:
        text = memory_text({**self.META, "group": True})
        self.assertIn("### この場の人たちも知っていること", text)

    def test_ownerless_has_one_section(self) -> None:
        text = memory_text({**self.META, "ownerless": True})
        self.assertNotIn("###", text, "沒有對方 ⇒ 不分段")
        self.assertIn("あなたが前から知っていること", text)

    def test_missing_flags_default_to_only_hers(self) -> None:
        text = memory_text({"recall": ["主人は寒いのが苦手"]})
        self.assertNotIn("### 相手も知っていること", text)
        self.assertIn("### あなただけが知っていること", text)

    def test_switch_off_restores_old_header(self) -> None:
        with mock.patch.dict(os.environ, {"BLUEAGENT_GROUND_TAGS": "0"}):
            text = memory_text(dict(self.META))
        self.assertIn(OLD_HEADER, text)
        self.assertNotIn("###", text)

    def test_no_recall_no_block(self) -> None:
        self.assertEqual(memory_text({"recall": ["  "], "recall_shared": [True]}), "")


class TestCollisionPrompts(unittest.TestCase):
    def test_prethink_answers_first(self) -> None:
        from agent.brain.runtime.prethink import PROMPT

        self.assertIn("まずそれに応じる", PROMPT)
        self.assertNotIn("次に相手が口を開いたら、自分はまず何を言いたい", PROMPT,
                         "不再預排開場（話題碰撞）")

    def test_notes_put_their_words_first(self) -> None:
        request = BrainRequest(thread_id="th_x", entity="mira", principal="owner",
                               text="ねえ", system="persona",
                               meta={"subagent_notes": ["さっきの表のことを話したい"]})
        notes = [str(item.get("content") or "") for item in build_messages(request)
                 if "[[notes]]" in str(item.get("content") or "")]
        self.assertEqual(len(notes), 1)
        self.assertIn("相手の話が先", notes[0])


class TestUnanswered(unittest.TestCase):
    """第 4 步（owner 2026-11-09 改定）：主人還沒回也可以再說，但**不能重複**還沒回的那一條。"""

    T0 = 1_800_000_000.0

    def gate(self, **kwargs):
        from agent.brain.runtime.proactive import ProactiveGate

        kwargs.setdefault("min_gap_s", 0)
        kwargs.setdefault("daily_max", 99)
        return ProactiveGate(quiet_start=0, quiet_end=0, clock=lambda: self.T0, **kwargs)

    def send(self, gate, text, at):
        return gate.maybe_send("mira", text, now=at)

    def test_without_heard_signal_rule_is_off(self) -> None:
        gate = self.gate()
        self.assertTrue(self.send(gate, "ひとつめ", self.T0)["sent"])
        self.assertTrue(self.send(gate, "ふたつめ、ぜんぜん違う話", self.T0 + 60)["sent"],
                        "沒有任何「主人說過話」的信號 ⇒ 舊行為")

    def test_can_send_again_but_not_the_same_thing(self) -> None:
        gate = self.gate()
        gate.note_heard("mira", "owner", at=self.T0 - 600)
        self.assertTrue(self.send(gate, "観測表できたよ、見てほしいな", self.T0)["sent"])
        self.assertEqual(gate.unanswered("mira", now=self.T0 + 60), [self.T0])
        same = self.send(gate, "観測表できたから、見てほしい", self.T0 + 1800)
        self.assertEqual(same, {"sent": False, "reason": "repeat_unanswered"}, "換句話說也算重複")
        other = self.send(gate, "雨が降ってきたね、傘ある？", self.T0 + 1900)
        self.assertTrue(other["sent"], "別的事 ⇒ 可以說")
        self.assertEqual(gate.unanswered_texts("mira", now=self.T0 + 2000),
                         ["観測表できたよ、見てほしいな", "雨が降ってきたね、傘ある？"])
        gate.note_heard("mira", "owner", at=self.T0 + 2000)
        self.assertEqual(gate.unanswered_texts("mira", now=self.T0 + 2100), [], "回了就清")
        self.assertTrue(self.send(gate, "観測表できたから、見てほしい", self.T0 + 2400)["sent"],
                        "回了之後就不算「還沒回的重複」（一般去重另算）")

    def test_state_survives_restart(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "proactive.json")
            first = self.gate(state_path=path)
            first.note_heard("mira", "owner", at=self.T0 - 600)
            self.send(first, "観測表できたよ", self.T0)
            second = self.gate(state_path=path)
            self.assertEqual(second.unanswered("mira", now=self.T0 + 60), [self.T0])
            self.assertEqual(second.unanswered_texts("mira", now=self.T0 + 60), ["観測表できたよ"])

    def test_old_sends_do_not_count(self) -> None:
        gate = self.gate()
        gate.note_heard("mira", "owner", at=self.T0 - 600)
        self.send(gate, "観測表できたよ", self.T0)
        self.assertEqual(gate.unanswered("mira", now=self.T0 + 25 * 3600), [],
                         "一天前那條不算（主人回來時已經是另一件事）")

    def test_last_heard_callable(self) -> None:
        heard = {"at": self.T0 - 600}
        gate = self.gate(last_heard=lambda entity, principal: heard["at"])
        self.send(gate, "観測表できたよ", self.T0)
        self.assertEqual(gate.unanswered("mira", now=self.T0 + 60), [self.T0])
        heard["at"] = self.T0 + 30
        self.assertEqual(gate.unanswered("mira", now=self.T0 + 60), [])

    def test_turn_service_notes_heard(self) -> None:
        from agent.brain.compose.turns import TurnService

        calls = []

        class Gate:
            def note_heard(self, entity, principal, *, at=None):
                calls.append((entity, principal, at))

        service = TurnService(runtime=None, threads=None, pending_store=None, lives=None,
                              principals=None, proactive=Gate())
        service._note_heard("mira", "owner", 123.0)
        self.assertEqual(calls, [("mira", "owner", 123.0)])

    def test_turn_service_translates_account_id_before_heard(self) -> None:
        """平台那條進來的 principal 是帳號 id（`acct_owner`）⇒ 記「回了」要用大腦 principal（`owner`）。"""
        import hashlib
        import json
        import pathlib
        import tempfile

        from agent.brain.compose.turns import TurnService
        from agent.brain.runtime.identity.accounts import AccountStore

        calls = []

        class Gate:
            def note_heard(self, entity, principal, *, at=None):
                calls.append((entity, principal, at))

        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / "accounts.json"
            path.write_text(json.dumps({"version": 1, "accounts": {
                "acct_owner": {"account_id": "acct_owner", "role": "master", "principal": "owner",
                               "token_sha256": hashlib.sha256(b"t").hexdigest()},
            }}), encoding="utf-8")
            service = TurnService(runtime=None, threads=None, pending_store=None, lives=None,
                                  principals=None, proactive=Gate(),
                                  accounts=AccountStore(path))
            service._note_heard("mira", "acct_owner", 456.0)
            service._note_heard("mira", "owner", 789.0)
        self.assertEqual(calls, [("mira", "owner", 456.0), ("mira", "owner", 789.0)])

    def test_composer_and_monologue_see_unanswered(self) -> None:
        from agent.brain.compose.heartbeat_compose import UNANSWERED_NOTE, said_lines, talk_digest

        gate = self.gate()
        gate.note_heard("mira", "owner", at=self.T0 - 600)
        self.send(gate, "観測表できたよ", self.T0)
        lines = said_lines(gate, "mira")
        self.assertTrue(any(UNANSWERED_NOTE in line and "観測表できたよ" in line for line in lines))
        gate.note_heard("mira", "owner", at=self.T0 + 10)
        self.assertFalse(any(UNANSWERED_NOTE in line for line in said_lines(gate, "mira")))
        self.assertIn(UNANSWERED_NOTE, talk_digest([], "観測表できたよ", now=self.T0, unanswered=True))
        self.assertNotIn(UNANSWERED_NOTE, talk_digest([], "観測表できたよ", now=self.T0))


class TestChannelSwitch(unittest.TestCase):
    """H3：同一條線橫跨端；這一句跟上一句不在同一個端 ⇒ `[[clock]]` 提醒帶上「在哪裡說的」。"""

    def thread(self, *turns):
        from types import SimpleNamespace as NS

        return NS(transcript=[NS(role=role, speaker=speaker, source=source)
                              for role, speaker, source in turns], turns=len(turns),
                  last_active_at=0.0)

    def test_switch_is_named(self) -> None:
        from agent.brain.runtime.inner.timeframe import Timeline, channel_line

        thread = self.thread(("user", "owner", "discord:123"), ("assistant", "mira", "discord:123"),
                             ("user", "owner", "app:cnv_x"))
        line = channel_line(thread)
        self.assertIn("いま アプリ から", line)
        self.assertIn("Discord で話したことは アプリ の画面には出ていない", line)
        self.assertIn(line, Timeline(clock=lambda: 1000.0).block("mira", thread))

    def test_same_channel_or_unknown_is_silent(self) -> None:
        from agent.brain.runtime.inner.timeframe import channel_line

        self.assertEqual(channel_line(self.thread(("user", "owner", "app:a"), ("user", "owner", "phone:owner"))),
                         "", "app 與 phone 都是アプリ")
        self.assertEqual(channel_line(self.thread(("user", "owner", "app:a"))), "")
        self.assertEqual(channel_line(self.thread(("user", "alice", "discord:1"),
                                                  ("user", "owner", "app:a"))), "", "只比同一個人")
        self.assertEqual(channel_line(self.thread(("user", "owner", "seed"), ("user", "owner", "app:a"))), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
