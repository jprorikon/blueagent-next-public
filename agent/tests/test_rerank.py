#!/usr/bin/env python3
"""檢索重排（rerank）：向量粗篩，LLM 定生死。

為什麼要這一層（實測）：jina-clip-v2 給「小説 ↔ ラノベ」0.92，但邊緣命中落在 0.55–0.62：
門檻調低會放進雜訊（「おはよう」0.59）、調高又漏掉真命中（「佐々木とピーちゃん」0.59）。
交給 LLM 逐條判「是不是同一件事」就沒有這個取捨。

判據：
  ① 換句話說要算同一件事（ラノベ／小説／轻小说／novel）；
  ② 話題相近但不是同一件事 ⇒ 砍掉；
  ③ 模型輸出壞掉／呼叫失敗 ⇒ **fail-open**（原樣保留），不讓檢索變空；
  ④ 只回「索引」不回新句子（不能讓模型編造記憶）；
  ⑤ 候選上限 8 條、每條截 160 字（成本可控）。
"""

from __future__ import annotations

import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.memory.rerank import MAX_CANDIDATES, Reranker, parse_keep


class TestParse(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(parse_keep('{"keep": [1, 3]}', 4), [0, 2])

    def test_json_with_prose(self):
        self.assertEqual(parse_keep('好的\n```json\n{"keep":[2]}\n```', 3), [1])

    def test_out_of_range_ignored(self):
        self.assertEqual(parse_keep('{"keep": [9]}', 3), [])

    def test_broken_returns_none(self):
        self.assertIsNone(parse_keep("我覺得都很像", 3))
        self.assertIsNone(parse_keep("", 3))


class TestPromptLanguage(unittest.TestCase):
    def test_prompt_sent_to_the_model_is_japanese(self):
        """★ review 2026-10-28：判定指令以前整段繁中（「你是記憶檢索的判官」）。"""
        from prompt_lang import chinese_hits
        seen: list[str] = []

        def call(prompt: str) -> str:
            seen.append(prompt)
            return '{"keep": [1]}'

        rows = [{"text": "ラノベを読んでいる"}, {"text": "おはよう"}, {"text": "焙茶が好き"}]
        Reranker(call, min_candidates=1).rerank("小説の話", rows, limit=3)
        self.assertTrue(seen)
        self.assertEqual(chinese_hits(seen[0]), [])
        self.assertIn('{"keep"', seen[0])


class TestRerank(unittest.TestCase):
    def rows(self):
        return [{"text": "今、小説読んでるよ。", "score": 0.9},
                {"text": "おはよう", "score": 0.6},
                {"text": "佐々木とピーちゃんというラノベ。", "score": 0.58}]

    def test_keeps_only_relevant(self):
        calls = []

        def call(prompt):
            calls.append(prompt)
            return '{"keep": [1, 3]}'

        reranker = Reranker(call)
        out = reranker.rerank("ラノベ読んでる？", self.rows(), limit=3)
        self.assertEqual([row["text"][:2] for row in out], ["今、", "佐々"])
        self.assertEqual(len(calls), 1)
        self.assertIn("1. 今、小説読んでるよ。", calls[0])
        self.assertIn("ラノベ読んでる？", calls[0])

    def test_fail_open_on_error(self):
        def boom(prompt):
            raise RuntimeError("deepseek down")

        out = Reranker(boom).rerank("q", self.rows(), limit=3)
        self.assertEqual(len(out), 3)

    def test_fail_open_on_garbage(self):
        out = Reranker(lambda prompt: "嗯……都很像吧").rerank("q", self.rows(), limit=3)
        self.assertEqual(len(out), 3)

    def test_skips_when_too_few(self):
        calls = []
        Reranker(lambda prompt: calls.append(prompt) or '{"keep": [1]}').rerank(
            "q", self.rows()[:1], limit=3)
        self.assertEqual(calls, [], "只有一條就不用花錢問模型")

    def test_timeout_falls_back_to_original(self):
        import time as _time

        def slow(prompt):
            _time.sleep(0.4)
            return '{"keep": [1]}'

        reranker = Reranker(slow, timeout_s=0.05)
        out = reranker.rerank("q", self.rows(), limit=3)
        self.assertEqual(len(out), 3, "超時要回原排序，不是回空")
        self.assertEqual(reranker.snapshot()["timeouts"], 1)

    def test_cache_skips_the_second_question(self):
        calls = []

        def call(prompt):
            calls.append(prompt)
            return '{"keep": [1, 2]}'

        reranker = Reranker(call)
        reranker.rerank("ラノベ", self.rows(), limit=3)
        reranker.rerank("ラノベ", self.rows(), limit=3)
        self.assertEqual(len(calls), 1, "同一問題＋同候選 60 秒內只問一次")
        self.assertEqual(reranker.snapshot()["cache_hits"], 1)

    def test_candidate_cap(self):
        many = [{"text": f"第 {index} 條"} for index in range(1, 20)]
        seen = {}

        def call(prompt):
            seen["prompt"] = prompt
            return '{"keep": [1]}'

        Reranker(call).rerank("q", many, limit=5)
        self.assertIn(f"{MAX_CANDIDATES}. ", seen["prompt"])
        self.assertNotIn(f"{MAX_CANDIDATES + 1}. ", seen["prompt"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
