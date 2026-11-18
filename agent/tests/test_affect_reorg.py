#!/usr/bin/env python3
"""情感系統整理（docs/PLAN-affect-reorg-20261109.md）。

判據：
  ① 她自己這邊的事（活動、公共區反饋、等回音）走核心的 `outcome`，**真的進心情**，
     而且不重設跟主人的閒置計時（以前走 `appraise()` 一律 SOURCE_NOT_FOUND、還回報 ok）。
  ② 主人每句話的固定推力會折半（P1）。
  ③ 主要原因判得出「主人看不看得見」；低落而看不見 ⇒ 回話與組句都要一句帶過原因（P4）。
  ④ 已讀 3 小時沒回 ⇒ 每條一次 `ignored`；沒讀的不算（P3）。
  ⑤ 被 `@` 的拿起率乘心情係數（P6）。
"""

from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace as NS

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.inner.affect import AffectService


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.affect = AffectService(self.tmp.name)


class TestOutcome(Base):
    def test_activity_really_lands_and_idle_is_untouched(self) -> None:
        from agent.brain.runtime.inner.activity_affect import appraise_activity

        result = appraise_activity(self.affect, "mira", "study", "資料を読んだ")
        self.assertTrue(result["ok"], result)
        rows = self.affect.timeline("mira")
        self.assertEqual(len(rows), 1, "以前這一筆永遠寫不進去")
        self.assertEqual(rows[0]["target"], "mira")
        self.assertEqual(self.affect.idle_s("mira"), 0.0, "她自己的事不算「有人跟她說話」")

    def test_failure_is_reported_not_swallowed(self) -> None:
        from agent.brain.runtime.inner.activity_affect import appraise_activity

        broken = NS(outcome=lambda *a, **k: {"ok": False, "error": "INVALID_INPUT"})
        self.assertFalse(appraise_activity(broken, "mira", "study", "x")["ok"])

    def test_public_step_bonus_is_small(self) -> None:
        from agent.brain.compose.life_wiring import make_activity_affect

        on_public = make_activity_affect(self.affect)
        on_public("mira", "観測表を一枚", NS(id="act_1"))
        on_public("mira", "観測表をもう一枚", NS(id="act_1"))
        rows = self.affect.timeline("mira")
        # 2026-11-09：delta 進核心前會按信心縮放（稽核第 3 組）⇒ 0.04 × 0.6 = 0.024
        self.assertEqual([(row["kind"], round(row["delta_valence"], 3)) for row in rows],
                         [("pleasure", 0.024)], "同一件事的公開步驟只算一次")

    def test_only_finished_activities_or_setbacks_count(self) -> None:
        from agent.brain.runtime.inner.activity_affect import appraise_activity

        skipped = appraise_activity(self.affect, "mira", "study", "一歩目", finished=False)
        self.assertEqual(skipped.get("skipped"), "step")
        self.assertEqual(self.affect.timeline("mira"), [], "中途的步驟不記（瑣事不灌心情）")
        appraise_activity(self.affect, "mira", "study", "ここで卡住了", finished=False)
        self.assertEqual([row["kind"] for row in self.affect.timeline("mira")], ["frustration"],
                         "挫折要即時")
        appraise_activity(self.affect, "mira", "study", "まとめた", finished=True)
        self.assertEqual(len(self.affect.timeline("mira")), 2)

    def test_board_feedback(self) -> None:
        from agent.brain.compose.affect_wiring import board_to_affect

        self.assertTrue(board_to_affect(self.affect, "aster", {"kind": "board.like", "post_id": "p1",
                                                             "actor_id": "nori"})["ok"])
        self.assertTrue(board_to_affect(self.affect, "aster", {"kind": "board.reply", "post_id": "p1",
                                                             "actor_id": "owner"})["ok"])
        kinds = sorted((row["kind"], row["target"]) for row in self.affect.timeline("aster"))
        self.assertEqual(kinds, [("interest", "board-p1"), ("pleasure", "board-p1")])
        self.assertFalse(board_to_affect(self.affect, "aster", {"kind": "post.created"})["ok"])

    def test_owner_interest_is_dampened(self) -> None:
        deltas = []
        for index in range(5):
            noted = self.affect.note_message("mira", f"ねえ{index}", speaker="owner")
            self.affect.appraise("mira", kind="interest", delta_v=0.18, delta_a=0.28,
                                 source_event_id=noted["event_id"], dampen=True)
        for row in self.affect.timeline("mira"):
            deltas.append(row["delta_valence"])
        # 2026-11-09：進核心前按信心 0.6 縮放 ⇒ 全額 0.108、折半 0.054（比例不變）
        self.assertIn(0.054, deltas, "第 4 次起折半")
        self.assertIn(0.108, deltas)


class TestCause(Base):
    def test_hidden_and_visible(self) -> None:
        from agent.brain.runtime.inner.mood_cause import main_cause

        self.assertEqual(main_cause(self.affect, "mira"), {})
        self.affect.outcome("mira", kind="frustration", delta_v=-0.2, delta_a=0.1,
                            event_id="x1", target_type="task", target_id="mira")
        cause = main_cause(self.affect, "mira")
        self.assertEqual((cause["key"], cause["hidden"], cause["down"]), ("self_down", True, True))
        self.assertIn("作業", cause["words"])
        noted = self.affect.note_message("mira", "ねえ聞いて", speaker="owner")
        self.affect.appraise("mira", kind="interest", delta_v=-0.5, delta_a=0.1,
                             source_event_id=noted["event_id"])
        self.assertEqual(main_cause(self.affect, "mira")["key"], "owner", "主人那句最大 ⇒ 看得見")

    def test_waiting_and_board_keys(self) -> None:
        from agent.brain.runtime.inner.mood_cause import _target_kind

        self.assertEqual(_target_kind({"target": "waiting", "delta_valence": -0.07}), "waiting")
        self.assertEqual(_target_kind({"target": "board-p1", "delta_valence": 0.05}), "board_up")
        self.assertEqual(_target_kind({"target": "owner", "kind": "concern",
                                       "delta_valence": -0.2}), "absence")

    def test_words(self) -> None:
        from agent.brain.runtime.inner.mood_words import mood_block, mood_note, template

        hidden = {"hidden": True, "words": "自分の作業がうまくいかなかった"}
        block = mood_block({"label": "しょんぼり", "arousal": 0.1}, cause=hidden)
        self.assertIn("理由は相手に見えていない", block)
        self.assertIn("自分の作業がうまくいかなかった", block)
        visible = mood_block({"label": "しょんぼり", "arousal": 0.1}, cause={"hidden": False})
        self.assertNotIn("見えていない", visible, "看得見 ⇒ 照舊")
        self.assertEqual(mood_block({"label": "ごきげん", "arousal": 0.3}, cause=hidden),
                         mood_block({"label": "ごきげん", "arousal": 0.3}), "只管低落")
        self.assertIn("理由：自分の作業がうまくいかなかった", mood_note("いらいら", hidden))
        self.assertEqual(template("しょんぼり"), template("ふつう"))

    def test_old_import_path_still_works(self) -> None:
        from agent.brain.runtime.inner.affect import mood_block as old
        from agent.brain.runtime.inner.mood_words import mood_block as new

        self.assertIs(old, new)


class TestIgnored(unittest.TestCase):
    T0 = 1_800_000_000.0

    def test_read_and_unanswered_for_three_hours_once(self) -> None:
        from agent.brain.runtime.proactive import ProactiveGate

        gate = ProactiveGate(min_gap_s=0, daily_max=99, quiet_start=0, quiet_end=0,
                             clock=lambda: self.T0)
        gate.note_heard("aster", "owner", at=self.T0 - 600)
        gate.maybe_send("aster", "観測表できたよ", now=self.T0,
                        publish=lambda principal, frame: gate.note_delivered(
                            "aster", principal, conversation_id="c", message_id="m1", seq=4,
                            at=frame["at"]))
        self.assertEqual(gate.stale_reads("aster", now=self.T0 + 4 * 3600, after_s=3 * 3600), [],
                         "沒讀 ⇒ 不算")
        gate.note_read("aster", "owner", conversation_id="c", seq=4)
        self.assertEqual(gate.stale_reads("aster", now=self.T0 + 3600, after_s=3 * 3600), [],
                         "讀了但還不到 3 小時")
        rows = gate.stale_reads("aster", now=self.T0 + 4 * 3600, after_s=3 * 3600)
        self.assertEqual([row["message_id"] for row in rows], ["m1"])
        self.assertEqual(gate.stale_reads("aster", now=self.T0 + 5 * 3600, after_s=3 * 3600), [],
                         "每條只一次")

    def test_heartbeat_turns_it_into_one_episode(self) -> None:
        from agent.brain.runtime.heartbeat import Heartbeat

        calls = []

        class Gate:
            def stale_reads(self, entity, *, principal, now):
                return [{"message_id": "m1"}]

            def allows(self, entity, *, now, principal):
                return False, "min_gap"

        class Affect:
            def advance(self, entity): return {}
            def snapshot(self, entity): return {"arousal": 0.5}
            def mood(self, entity, at=None): return {"label": "ふつう", "propensity": 0.0}
            def outcome(self, entity, **kwargs): calls.append(kwargs)

        beat = Heartbeat(entity="aster", affect=Affect(), gate=Gate(), clock=lambda: self.T0)
        beat.arousal_pacing = False
        beat.tick()
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["kind"], calls[0]["delta_v"], calls[0]["target_id"]),
                         ("concern", -0.07, "waiting"))


class TestCalledFactor(unittest.TestCase):
    def test_factor_and_wiring(self) -> None:
        from agent.adapter.app_group import GroupAttention
        from agent.brain.compose.affect_wiring import wire
        from agent.brain.runtime.inner.mood_words import called_factor

        self.assertEqual(called_factor("しょんぼり"), 0.85)
        self.assertEqual(called_factor("ごきげん"), 1.0)
        attention = GroupAttention()
        adapter = NS(group_attention=attention, on_board=lambda *a: None)
        affect = NS(mood=lambda entity, at=None: {"label": "しょんぼり"})
        wire(NS(affect=affect), [adapter])
        self.assertEqual(attention.mood("aster", 0.0), 0.85)
        wire(NS(affect=affect), [adapter])
        self.assertTrue(getattr(adapter.on_board, "_affect_wired", False), "重接不疊兩層")


if __name__ == "__main__":
    unittest.main(verbosity=2)
