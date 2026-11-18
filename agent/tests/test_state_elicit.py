#!/usr/bin/env python3
"""P1–P3（SPIN 三段式的後兩段）：

· P1 狀態引出：core × 情境 → transient state（群聊／多人／TRPG 才跑，私聊不加延遲）
· P2 狀態 → AEM：valence/intensity 換成 delta_v/delta_a，心情與「讀法」同一條曲線
· P3 條件區分：informational（在問事）vs normative（公開表態）⇒ 後者不搶話

判據：
  ① 條件分類：問句/疑問詞 → informational；純評價 → normative；兩者混合 → informational 或 mixed；
  ② 點名一律 informational；group 裡「她剛說完接著講」遇到 normative ⇒ 不算對她說；
  ③ 狀態引出：形狀不對回 None；prompt 只放「讀法」不放 trait 數字；只在群聊/TRPG 觸發；
  ④ P2：valence 正 → delta_v 正、intensity 高 → delta_a 高，且 label 對到 episode_kind。
"""

from __future__ import annotations

import json
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.persona.state_elicit import (elicit_state, parse_state, prompt_block,
                                                 should_elicit)
from agent.brain.domain.social_condition import (INFORMATIONAL, MIXED, NORMATIVE,
                                                    classify, has_substance, should_answer)

GOOD = {
    "salient_cues": ["オーナーが論文の著者名を見ている", "場が和んでいる"],
    "active_dispositions": [{"trigger": "teasing", "tendency": "軽くからかう"}],
    "emotion": {"label": "興味", "intensity": 0.7, "valence": 0.5},
    "concerns": ["茶化しすぎない"],
    "social_condition": "informational",
    "intent": "chime_in",
    "stance_hint": "軽く茶化してから本題に入る",
}


class TestCondition(unittest.TestCase):
    def test_question_is_informational(self):
        self.assertEqual(classify("明日の予定は？"), INFORMATIONAL)
        self.assertEqual(classify("これ何"), INFORMATIONAL)
        self.assertEqual(classify("教えてよ"), INFORMATIONAL)

    def test_opinion_is_normative(self):
        self.assertEqual(classify("只用來玩遊戲真是浪費顯卡（"), NORMATIVE)
        self.assertEqual(classify("我覺得這設計不太對"), NORMATIVE)

    def test_addressed_is_always_informational(self):
        self.assertEqual(classify("只用來玩遊戲真是浪費", addressed=True), INFORMATIONAL)

    def test_question_plus_opinion_is_informational_or_mixed(self):
        self.assertIn(classify("這不是很浪費嗎？"), {INFORMATIONAL, MIXED})

    def test_plain_statement_is_mixed(self):
        self.assertEqual(classify("今日は雨だね"), MIXED)

    def test_should_answer_rules(self):
        self.assertTrue(should_answer(INFORMATIONAL, mentioned=False, continuation=True,
                                       has_substance=True))
        self.assertFalse(should_answer(NORMATIVE, mentioned=False, continuation=True,
                                       has_substance=True), "公開表態不該搶答")
        self.assertTrue(should_answer(NORMATIVE, mentioned=True, continuation=False,
                                       has_substance=True), "點名了就該答")
        self.assertFalse(should_answer(MIXED, mentioned=False, continuation=False,
                                       has_substance=True))

    def test_has_substance(self):
        self.assertFalse(has_substance("。"))
        self.assertFalse(has_substance("ｗ"))
        self.assertTrue(has_substance("ゲームとった"))


class TestStateParse(unittest.TestCase):
    def test_parse_good(self):
        state = parse_state(json.dumps(GOOD, ensure_ascii=False))
        self.assertEqual(state["emotion"]["label"], "興味")
        self.assertEqual(state["social_condition"], INFORMATIONAL)
        self.assertEqual(state["intent"], "chime_in")
        self.assertEqual(len(state["salient_cues"]), 2)

    def test_bad_values_are_clamped_not_fatal(self):
        raw = json.loads(json.dumps(GOOD))
        raw["emotion"] = {"label": "興味", "intensity": 9, "valence": -5}
        state = parse_state(json.dumps(raw))
        self.assertEqual(state["emotion"]["intensity"], 1.0)
        self.assertEqual(state["emotion"]["valence"], -1.0)

    def test_unknown_condition_and_intent_fall_back(self):
        raw = json.loads(json.dumps(GOOD))
        raw["social_condition"] = "weird"
        raw["intent"] = "dance"
        state = parse_state(json.dumps(raw))
        self.assertEqual(state["social_condition"], MIXED)
        self.assertEqual(state["intent"], "answer")

    def test_empty_or_garbage_returns_none(self):
        self.assertIsNone(parse_state(""))
        self.assertIsNone(parse_state("わかりません"))
        self.assertIsNone(parse_state(json.dumps({"emotion": {"label": ""}})))

    def test_prompt_block_has_reading_not_numbers(self):
        block = prompt_block(parse_state(json.dumps(GOOD, ensure_ascii=False)))
        self.assertIn("目に入ったこと", block)
        self.assertIn("興味", block)
        self.assertNotIn("0.7", block, "不要把 trait/intensity 數字塞進 prompt")
        self.assertNotIn("{", block)


class TestElicitGate(unittest.TestCase):
    def test_group_or_trpg_only(self):
        self.assertTrue(should_elicit(group=True, source="discord:g1", enabled=None))
        self.assertTrue(should_elicit(group=False, source="trpg:table-1", enabled=None))
        self.assertFalse(should_elicit(group=False, source="discord:dm", enabled=None),
                         "私聊不跑（省延遲）")

    def test_env_can_force_all_or_off(self):
        import os
        os.environ["BLUEAGENT_STATE_ELICIT"] = "all"
        self.addCleanup(os.environ.pop, "BLUEAGENT_STATE_ELICIT", None)
        self.assertTrue(should_elicit(group=False, source="discord:dm", enabled=None))
        self.assertFalse(should_elicit(group=True, source="x", enabled=False))

    def test_elicit_without_call_is_none(self):
        self.assertIsNone(elicit_state({"trait_dimensions": {}}, text="やあ", call=None))

    def test_elicit_uses_call(self):
        core = {"trait_dimensions": {"teasing": 0.9},
                "disposition_rules": [{"trigger": "teasing", "tendency": "からかう",
                                       "strength": 0.9}],
                "portrait": "小悪魔"}
        state = elicit_state(core, text="これ何？", scene="group",
                             call=lambda _prompt: json.dumps(GOOD, ensure_ascii=False))
        self.assertIsNotNone(state)
        self.assertIn("elapsed_ms", state)

    def test_elicit_failure_is_silent(self):
        def boom(_prompt):
            raise RuntimeError("模型掛了")
        core = {"trait_dimensions": {}, "disposition_rules": [], "portrait": "x"}
        self.assertIsNone(elicit_state(core, text="やあ", call=boom, log=lambda _m: None))


class TestStateToAffect(unittest.TestCase):
    def test_positive_state_raises_valence_and_arousal(self):
        state = make_state()
        self.addCleanup(state.shutdown)
        affect = state.app.affect
        before = affect.snapshot("mira").get("affect") or {}
        affect.note_message("mira", "これ何？", speaker="owner")
        result = affect.note_state("mira", parse_state(json.dumps(GOOD, ensure_ascii=False)),
                                   source_event_id="ev-1")
        self.assertTrue(result.get("ok"), result)
        after = affect.snapshot("mira").get("affect") or {}
        self.assertGreater(after.get("valence", 0), before.get("valence", 0))
        self.assertGreater(after.get("arousal", 0), 0.0)

    def test_negative_state_pushes_valence_down(self):
        state = make_state()
        self.addCleanup(state.shutdown)
        affect = state.app.affect
        affect.note_message("mira", "…", speaker="owner")
        sad = {"emotion": {"label": "不安", "intensity": 0.9, "valence": -0.8},
               "social_condition": "normative", "intent": "stay_quiet"}
        affect.note_state("mira", sad, source_event_id="ev-2")
        view = (affect.snapshot("mira").get("affect") or {})
        self.assertLessEqual(view.get("valence", 0), 0.0)


class TestStageWiring(unittest.TestCase):
    def test_state_stage_runs_only_in_group_with_core(self):
        from agent.brain.compose.stages import StateElicitStage
        stage = StateElicitStage(cores={"mira": {"trait_dimensions": {},
                                                    "disposition_rules": [],
                                                    "portrait": "x"}},
                                 call=lambda _prompt: json.dumps(GOOD, ensure_ascii=False),
                                 log_path=pathlib.Path(tempfile.mkdtemp()) / "state.jsonl")
        self.assertEqual(stage.name, "state.elicit")
        self.assertEqual(stage.snapshot()["elicited"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
