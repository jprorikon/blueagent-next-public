#!/usr/bin/env python3
"""自傳式記憶（提案⑤）＋獨白粒度（提案⑥）＋記憶注意力場（提案）。

三件都是 owner 2026-11-16「帮我做了」指定的：
  · 自傳式記憶：`data/self/<entity>/self-model.md` 兩段、每行必須帶 `〔fact:id〕`，
    被取代的事實自然消失（不寫人格卡）。
  · 獨白粒度：多 `hold`／`waiting`／`next` 三個欄位，`waiting` 必須與台帳 `locked` 一致。
  · 注意力場：按焦點開窗（准入）＋ gain 調制（排序），且**不得動可見性**。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "agent" / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from agent.brain.runtime.inner import self_model  # noqa: E402
from agent.brain.runtime.inner.monologue import Monologue, _parse_reply  # noqa: E402
from agent.brain.runtime.memory import attention_field  # noqa: E402

NOW = time.mktime(time.strptime("2026-11-16 12:00", "%Y-%m-%d %H:%M"))


def row(fact_id, text, *, kind="state", layer="pair", age_days=1.0, superseded_by=""):
    return {"id": fact_id, "text": text, "kind": kind, "layer": layer,
            "source": "distill", "created_at": NOW - age_days * 86400.0,
            "superseded_by": superseded_by}


class SelfModelCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="ba-self-")
        self.addCleanup(self.dir.cleanup)
        self.root = pathlib.Path(self.dir.name)

    def test_every_line_carries_a_fact_id(self):
        stable, recent = self_model.build([row(11, "主人のことは「オーナー」と呼ぶ"),
                                          row(12, "", kind="state")],
                                         now=NOW)
        self.assertEqual(len(stable), 1, "空文字的丢掉")
        self.assertIn("〔fact:11〕", stable[0])

    def test_rebuild_writes_two_sections_and_read_back(self):
        result = self_model.rebuild(self.root, "nori", now=NOW, rows=[
            row(21, "記録は通し番号から書く"),
            row(22, "判定表をまとめ直した", kind="event", age_days=2)])
        self.assertTrue(pathlib.Path(result["path"]).exists())
        body = pathlib.Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn(self_model.STABLE_HEADER, body)
        self.assertIn(self_model.RECENT_HEADER, body)
        data = self_model.read(self.root, "nori")
        self.assertEqual(len(data["stable"]), 1)
        self.assertEqual(len(data["recent"]), 1)

    def test_superseded_facts_disappear(self):
        stable, _recent = self_model.build([row(31, "以前の呼び方", superseded_by="32")], now=NOW)
        self.assertEqual(stable, [], "被取代的事實不進自述（只讀 superseded_by 為空的）")

    def test_old_events_do_not_enter_recent(self):
        _stable, recent = self_model.build([row(41, "三週前のこと", kind="event", age_days=21)], now=NOW)
        self.assertEqual(recent, [], "「最近的我」只看最近 7 天")

    def test_prompt_block_is_empty_without_file(self):
        self.assertEqual(self_model.prompt_block(self.root, "aster"), "")

    def test_prompt_block_after_rebuild(self):
        self_model.rebuild(self.root, "aster", now=NOW, rows=[row(51, "星を見に行くのが好き")])
        block = self_model.prompt_block(self.root, "aster")
        self.assertIn("ずっとそういう人間", block)


class MonologueGranularityCase(unittest.TestCase):
    def test_parse_picks_the_three_new_fields(self):
        raw = ('{"text": "言いたいけど今は言わない", "action": "none", "why": "様子見", '
               '"hold": "相手が忙しそう", "waiting": "主人", "next": "表を作る"}')
        parsed = _parse_reply(raw)
        self.assertEqual(parsed["hold"], "相手が忙しそう")
        self.assertEqual(parsed["waiting"], "主人")
        self.assertEqual(parsed["next"], "表を作る")

    def make(self, reply: str) -> tuple[Monologue, list[dict]]:
        rows: list[dict] = []
        mono = Monologue(tempfile.mkdtemp(prefix="ba-mono-"), call=lambda prompt: reply,
                         enabled=True, clock=lambda: NOW)
        mono._append = lambda entity, item: rows.append(dict(item))   # 不落盤，只看內容
        return mono, rows

    def test_waiting_is_dropped_when_the_ledger_has_no_locked_item(self):
        mono, rows = self.make('{"text": "返事待ち", "action": "none", "why": "待ち", '
                               '"waiting": "主人"}')
        mono.beat("nori", facts={"clock": "12:00", "locked": []}, at=NOW)
        self.assertEqual(rows[0]["waiting"], "")
        self.assertEqual(rows[0]["waiting_source"], "dropped_no_locked")

    def test_waiting_survives_when_the_ledger_is_locked(self):
        mono, rows = self.make('{"text": "返事待ち", "action": "none", "why": "待ち", '
                               '"waiting": "主人"}')
        mono.beat("nori", facts={"clock": "12:00", "locked": ["判定表（主人の返事待ち）"]}, at=NOW)
        self.assertEqual(rows[0]["waiting"], "主人")
        self.assertEqual(rows[0]["waiting_source"], "ledger_locked")

    def test_next_only_kept_for_work_action(self):
        mono, rows = self.make('{"text": "思っただけ", "action": "none", "why": "x", "next": "表を作る"}')
        mono.beat("nori", facts={"clock": "12:00"}, at=NOW)
        self.assertEqual(rows[0]["next"], "")


class FakeFact:
    def __init__(self, payload):
        self.id = payload["id"]
        self.text = payload["text"]
        self.layer = payload.get("layer", "pair")
        self.audience = payload.get("audience")
        self.principal = payload.get("principal", "")
        self.created_at = payload.get("created_at", NOW)
        self.superseded_by = ""

    def is_active(self):
        return True


class AttentionFieldCase(unittest.TestCase):
    def test_focus_drops_herself(self):
        self.assertEqual(attention_field.focus_of({"owner", "nori"}, "nori"), {"owner"})
        self.assertEqual(attention_field.focus_of({"nori"}, "nori"), set())

    def test_gain_prefers_the_person_in_focus(self):
        fact = FakeFact({"id": 1, "text": "主人は辛いものが苦手", "audience": ["owner"]})
        value, parts = attention_field.gain(fact, {"owner"}, newest_at=NOW)
        self.assertGreater(value, 1.0)
        self.assertEqual(parts["overlap"], 1.0)
        world = FakeFact({"id": 2, "text": "世界のこと", "audience": None})
        self.assertEqual(attention_field.gain(world, {"owner"}, newest_at=NOW)[0], 1.0)

    def test_merge_dedupes_by_id(self):
        a, b = FakeFact({"id": 1, "text": "a"}), FakeFact({"id": 1, "text": "a"})
        c = FakeFact({"id": 2, "text": "c"})
        merged = attention_field.merge([a], [b, c])
        self.assertEqual([fact.id for fact in merged], [1, 2])

    def test_session_bonus_only_within_window(self):
        near = FakeFact({"id": 3, "text": "剛剛聊的", "audience": ["owner"], "created_at": NOW - 600})
        far = FakeFact({"id": 4, "text": "上個月的", "audience": ["owner"],
                        "created_at": NOW - 30 * 86400})
        self.assertGreater(attention_field.gain(near, {"owner"}, newest_at=NOW)[0],
                           attention_field.gain(far, {"owner"}, newest_at=NOW)[0])

    def test_stance_strength_raises_gain_for_related_fact_only(self):
        fact = FakeFact({"id": 5, "text": "主人の好み", "audience": ["owner"]})
        plain, _ = attention_field.gain(fact, {"owner"}, newest_at=NOW, stance_strength=0.0)
        strong, parts = attention_field.gain(fact, {"owner"}, newest_at=NOW, stance_strength=0.9)
        self.assertGreater(strong, plain)
        self.assertAlmostEqual(parts["stance"], 0.9, places=3)
        world = FakeFact({"id": 6, "text": "世界のこと", "audience": None})
        self.assertEqual(attention_field.gain(world, {"owner"}, newest_at=NOW,
                                              stance_strength=0.9)[1]["stance"], 0.0)

    def test_stance_strength_reads_the_service(self):
        class Axis:
            weight = 60.0

        class Stance:
            axes = [Axis(), Axis()]

        class Service:
            def get(self, entity, member):
                return Stance() if member == "owner" else None

        self.assertAlmostEqual(attention_field.stance_strength(Service(), "aster", {"owner"}),
                               0.6, places=3)
        self.assertEqual(attention_field.stance_strength(Service(), "aster", {"nobody"}), 0.0)
        self.assertEqual(attention_field.stance_strength(None, "aster", {"owner"}), 0.0)


class UsageFeedbackCase(unittest.TestCase):
    def test_used_texts_requires_two_shared_bigrams(self):
        from agent.brain.runtime.memory import usage_feedback
        used = usage_feedback.used_texts(["主人は辛いものが苦手だ"], "辛いもの、苦手だよね")
        self.assertEqual(used, ["主人は辛いものが苦手だ"])
        self.assertEqual(usage_feedback.used_texts(["関係ない話"], "今日はいい天気"), [])

    def test_greeting_does_not_mark_long_facts_as_used(self):
        """實測坑（2026-11-16 19:52）：一句「こんばんは」把 5 條遊戲筆記記成「用到」。"""
        from agent.brain.runtime.memory import usage_feedback
        reply = "こんばんは、オーナー。……今日はちゃんと片付けてありますからね。褒めなくていいです。"
        facts = ["主人のことは「オーナー」と呼ぶ。",
                 "ゲーム開発部の部室で、モモイたちのボケを一晩中つっこんでいたら、翌朝ぐったりしていた。"]
        self.assertEqual(usage_feedback.used_texts(facts, reply), [],
                         "功能詞重疊不算「用到」")

    def test_one_turn_marks_at_most_three_facts(self):
        from agent.brain.runtime.memory import usage_feedback
        facts = [f"主人は辛いものが苦手だ（その{i}）" for i in range(6)]
        used = usage_feedback.used_texts(facts, "辛いものは苦手だよね、辛いもの")
        self.assertLessEqual(len(used), usage_feedback.MAX_FACTS)

    def test_coverage_is_measured_against_the_fact_not_the_reply(self):
        """複審（2026-11-16）：覆蓋率的分母是事實；回覆一長，真的用到的記憶也要照記。"""
        from agent.brain.runtime.memory import usage_feedback
        fact = "主人は辛いものが苦手だ"
        short = "辛いもの苦手だよね"
        long_ = ("辛いもの苦手だよね。じゃあ今夜は別の店にしよう。駅前のパスタ屋さん、前から気になってたんだ。"
                 "予約しておくから、七時に改札で待ち合わせね。遅れたら置いていくよ？")
        self.assertEqual(usage_feedback.used_texts([fact], short), [fact])
        self.assertEqual(usage_feedback.used_texts([fact], long_), [fact], "長回覆不漏記")

    def test_two_shared_words_inside_a_long_fact_are_not_enough(self):
        from agent.brain.runtime.memory import usage_feedback
        fact = "ゲーム開発部の部室で、モモイたちのボケを一晩中つっこんでいたら、翌朝ぐったりしていた。"
        self.assertEqual(usage_feedback.used_texts([fact], "部室にいる"), [],
                         "長事實只撞到兩個詞 ⇒ 覆蓋率不到 15%")

    def test_mark_writes_through_note_usage(self):
        from agent.brain.runtime.memory import usage_feedback

        class Ports:
            def __init__(self):
                self.calls = []

            def note_usage(self, texts, used, **scope):
                self.calls.append((list(texts), used))

        class Ctx:                     # 管線的 ctx：**沒有** `turn`（召回清單由呼叫端傳）
            ports = Ports()

        ctx = Ctx()
        self.assertEqual(usage_feedback.mark(ctx, "辛いものは苦手だよね",
                                             recall=["主人は辛いものが苦手だ", "無関係"]), 1)
        self.assertEqual(ctx.ports.calls, [(["主人は辛いものが苦手だ"], True)])

    def test_mark_finds_note_usage_under_ports_memory(self):
        """`ctx.ports` 是 AppPorts（只有 .memory）——只查第一层会静默不写（实测坑）。"""
        from agent.brain.runtime.memory import usage_feedback

        class Memory:
            def __init__(self):
                self.calls = []

            def note_usage(self, texts, used, **scope):
                self.calls.append((list(texts), used))

        class Ports:
            def __init__(self):
                self.memory = Memory()

        class Ctx:
            def __init__(self):
                self.ports = Ports()

        ctx = Ctx()
        self.assertEqual(usage_feedback.mark(ctx, "辛いものは苦手だよね", recall=["主人は辛いものが苦手だ"]), 1)
        self.assertEqual(len(ctx.ports.memory.calls), 1)


if __name__ == "__main__":
    unittest.main()
