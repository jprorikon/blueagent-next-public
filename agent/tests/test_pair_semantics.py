#!/usr/bin/env python3
"""pair / circle / consensus 的口徑（owner 2026-10-23 拍板）。

三條決定：
  1. 披露層只用**子集**：`在場的人（含她）⊆ 房間成員` 才可說；
  2. 記得但**不提**：不在房間裡的人在場時，那條記憶不進 prompt，她也不主動解釋；
  3. 她 ↔ 另一個角色 agent：**雙方各自按自己的角度落一份**（各自 entity 倉、各自視角）。

這個檔釘住「層數含她在內算」與「audience 一定含她」——
舊口徑（名冊 1 人＝private、2 人＝pair）在這裡被推翻：
  · 她 + 1 人 ⇒ pair（共 2）
  · 她 + ≥2 人 ⇒ circle
  · 秘密（SECRET_WORDS）⇒ private（知情者＋她，共 2）
"""

from __future__ import annotations

import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.memory.fact_rules import facts_for_turn
from agent.brain.memory.facts import Fact
from agent.brain.domain.thread import MemoryScope, TurnRecord

ME = "mira"


def scope(principal: str = "owner") -> MemoryScope:
    return MemoryScope(principal=principal, entity=ME, scene="together",
                       thread_id=f"th-{principal}")


def facts(principal: str, participants, text: str):
    turn = TurnRecord(turn_id="t1", role="user", text=text, source="chat",
                      at=0.0, speaker=principal)
    return facts_for_turn(scope(principal), turn, participants=participants)


class TestLayerCountingIncludesHer(unittest.TestCase):
    def test_she_plus_one_is_pair(self):
        out = facts("owner", ["owner"], "记住 我讨厌吵闹")
        self.assertTrue(out)
        for fact in out:
            self.assertEqual(fact.layer, "pair", fact.text)
            self.assertIn(ME, fact.audience)
            self.assertEqual(set(fact.audience), {ME, "owner"})

    def test_she_plus_two_is_circle(self):
        out = facts("owner", ["owner", "alice"], "记住 我讨厌吵闹")
        self.assertTrue(out)
        for fact in out:
            self.assertEqual(fact.layer, "circle", fact.text)
            self.assertEqual(set(fact.audience), {ME, "owner", "alice"})

    def test_secret_is_private_and_keeps_the_confidant(self):
        out = facts("owner", ["owner"], "记住 这是秘密，别告诉别人：下周有惊喜")
        self.assertTrue(out)
        for fact in out:
            self.assertEqual(fact.layer, "private", fact.text)
            self.assertEqual(set(fact.audience), {ME, "owner"})

    def test_consensus_still_adds_its_own_row(self):
        out = facts("owner", ["owner", "alice"], "记住 说好了下周一起吃饭")
        layers = {fact.layer for fact in out}
        self.assertIn("consensus", layers)
        self.assertIn("circle", layers)


class TestTwoLayers(unittest.TestCase):
    def test_recallable_is_always_true(self):
        fact = Fact(text="x", layer="pair", audience=frozenset({ME, "owner"}))
        self.assertTrue(fact.recallable())

    def test_disclosable_is_subset_only(self):
        fact = Fact(text="x", layer="pair", audience=frozenset({ME, "owner"}))
        self.assertTrue(fact.disclosable({ME, "owner"}))
        self.assertTrue(fact.disclosable({ME}))                 # 她一個人：想得起來
        self.assertFalse(fact.disclosable({ME, "alice"}))       # 房間外的人 ⇒ 不提
        self.assertFalse(fact.disclosable({ME, "owner", "alice"}))

    def test_world_is_always_disclosable(self):
        fact = Fact(text="x", layer="world", audience=None)
        self.assertTrue(fact.disclosable({ME, "stranger"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
