#!/usr/bin/env python3
"""P0：人格 core（SPIN 第 ① 段）——編譯、快取、貼進 prompt 的那一小段。

判據：
  ① 解析：模型輸出（含 ```code fence``／前後雜訊）能抽出合法 core；形狀不對回 None；
  ② 穩定：同一份人格檔 → 同一個 core hash；人格檔改了 → hash 變（快取失效）；
  ③ prompt 只放「肖像＋最強幾條 if-then」，**不放 JSON**（免得她變問卷腔）；
  ④ 沒有 core 時 prompt 一個字都不多（可回退）；
  ⑤ 編譯失敗不擋對話。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.messages import build_messages
from agent.brain.persona.core import (TRAIT_DIMENSIONS, compile_core, core_hash,
                                        load_core, parse_core, prompt_block, save_core)
from agent.brain.ports import BrainRequest

GOOD = {
    "trait_dimensions": {name: 0.5 for name in TRAIT_DIMENSIONS},
    "disposition_rules": [
        {"trigger": "teasing", "tendency": "軽く返す", "strength": 0.9},
        {"trigger": "being_ignored", "tendency": "自分から話しかける", "strength": 0.7},
        {"trigger": "praise", "tendency": "調子に乗る", "strength": 0.4},
    ],
    "default_decision_style": "直感先行",
    "portrait": "明るく挑発的な小悪魔。",
}


class TestParse(unittest.TestCase):
    def test_plain_json(self):
        core = parse_core(json.dumps(GOOD, ensure_ascii=False), soul_text="soul-A")
        self.assertIsNotNone(core)
        self.assertEqual(set(core["trait_dimensions"]), set(TRAIT_DIMENSIONS))
        self.assertEqual(core["disposition_rules"][0]["trigger"], "teasing")
        self.assertEqual(core["soul_hash"], parse_core(json.dumps(GOOD), soul_text="soul-A")["soul_hash"])

    def test_code_fence_and_chatter(self):
        raw = "はい、こちらです。\n```json\n" + json.dumps(GOOD) + "\n```\nどうぞ。"
        self.assertIsNotNone(parse_core(raw))

    def test_rules_are_sorted_by_strength(self):
        core = parse_core(json.dumps(GOOD))
        strengths = [row["strength"] for row in core["disposition_rules"]]
        self.assertEqual(strengths, sorted(strengths, reverse=True))

    def test_bad_shape_returns_none(self):
        self.assertIsNone(parse_core(""))
        self.assertIsNone(parse_core("ごめん、わかりません"))
        self.assertIsNone(parse_core(json.dumps({"trait_dimensions": {"fairness_sensitivity": 0.5}})))
        missing = dict(GOOD, disposition_rules=[])
        self.assertIsNone(parse_core(json.dumps(missing)), "沒有傾向規則就不算 core")

    def test_unknown_trigger_is_dropped_not_fatal(self):
        raw = json.loads(json.dumps(GOOD))
        raw["disposition_rules"].append({"trigger": "weather", "tendency": "x", "strength": 0.9})
        core = parse_core(json.dumps(raw))
        triggers = [row["trigger"] for row in core["disposition_rules"]]
        self.assertNotIn("weather", triggers)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())

    def test_roundtrip_and_hash(self):
        core = parse_core(json.dumps(GOOD), soul_text="soul-A")
        save_core(self.root, "mira", core)
        loaded = load_core(self.root, "mira", soul_text="soul-A")
        self.assertEqual(core_hash(loaded), core_hash(core))

    def test_soul_change_invalidates(self):
        core = parse_core(json.dumps(GOOD), soul_text="soul-A")
        save_core(self.root, "mira", core)
        self.assertIsNone(load_core(self.root, "mira", soul_text="soul-B"),
                          "人格檔改了 ⇒ core 失效（要重編譯）")

    def test_missing_file_is_none(self):
        self.assertIsNone(load_core(self.root, "nobody"))


class TestPromptBlock(unittest.TestCase):
    def test_block_has_portrait_and_top_rules_without_json(self):
        block = prompt_block(parse_core(json.dumps(GOOD)), max_rules=2)
        self.assertIn("小悪魔", block)
        self.assertIn("teasing", block)
        self.assertNotIn("{", block, "不能把 JSON 塞進 prompt")
        self.assertNotIn('"trait_dimensions"', block)
        self.assertEqual(block.count("・"), 2, f"只放最強的兩條：{block}")

    def test_empty_core_gives_empty_block(self):
        self.assertEqual(prompt_block(None), "")
        self.assertEqual(prompt_block({}), "")


class TestCompile(unittest.TestCase):
    def test_compile_uses_call_and_returns_core(self):
        core = compile_core("プロフィール", soul_text="soul-A",
                            call=lambda _prompt: json.dumps(GOOD))
        self.assertIsNotNone(core)
        self.assertEqual(core["soul_hash"], parse_core(json.dumps(GOOD), soul_text="soul-A")["soul_hash"])

    def test_compile_failure_is_silent(self):
        def boom(_prompt):
            raise RuntimeError("模型掛了")
        self.assertIsNone(compile_core("プロフィール", call=boom, log=lambda _m: None))


class TestPromptWiring(unittest.TestCase):
    def request(self, **meta) -> BrainRequest:
        base = {"soul_block": "[[soul:mira]] 人格檔 [[/soul]]"}
        base.update(meta)
        return BrainRequest(thread_id="t", entity="mira", principal="owner",
                            text="やあ", system="", meta=base)

    def test_core_block_appears_after_soul(self):
        """core 貼在人格檔之後（2026-11-02：兩者都在**固定前綴段**，不在最後一條訊息）。

        改的原因：人格檔（含 core／user.md）已移到固定段，才吃得到引擎的前綴快取；
        最後一條用戶訊息現在只帶「本輪易變段＋這句話」。
        """
        block = prompt_block(parse_core(json.dumps(GOOD)), max_rules=3)
        messages = build_messages(self.request(core_block=block))
        joined = "\n".join(str(item["content"]) for item in messages)
        soul_at = joined.find("[[soul:mira]]")
        core_at = joined.find("あなたの核")
        self.assertGreater(soul_at, -1)
        self.assertGreater(core_at, soul_at, "core 要貼在人格檔之後")
        self.assertNotIn("あなたの核", str(messages[-1]["content"]),
                         "人格段（含 core）應在固定前綴，不在本輪訊息裡")

    def test_no_core_means_no_extra_text(self):
        messages = build_messages(self.request())
        self.assertNotIn("あなたの核", str(messages[-1]["content"]))

    def test_human_turn_with_core_still_answers(self):
        state = make_state()
        self.addCleanup(state.shutdown)
        state.app.persona_cores["mira"] = parse_core(json.dumps(GOOD))
        result = state.send("おはよう", principal="owner")
        self.assertTrue(state.reply_text(result), "有 core 也要正常回話")
        self.assertEqual(state.app.prompt_stats.get("core_hash"), core_hash(
            state.app.persona_cores["mira"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
