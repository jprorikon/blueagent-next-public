#!/usr/bin/env python3
"""語意檢索（混合排序）：換個說法也要找得到（owner 2026-10-23「直接 3」）。

背景（實測）：生產的語意層原本靠 Hindsight（`192.168.50.36:8897`），那台機已離線，
適配器把錯誤吞掉 ⇒ 召回全程退化成**字面 bigram**。於是「ラノベ／小説／小说／novel」
互相都搜不到；而且 owner 說話常中日交雜、朋友還會用英文與繁體。

判據：
  ① 語意命中：字面 0 重疊但語意近 ⇒ 要能找出來（用假 embedder 釘住機制）；
  ② 純字面模式（沒有 embedder）行為不變（可回退）；
  ③ 披露規則不被語意繞過：房間裡有不在場的人 ⇒ 那一筆仍然不回；
  ④ 語意門檻：完全無關的句子不會因為「都有點像」就回一堆；
  ⑤ 向量倉冪等：同樣文本重複 sync 不會重嵌。
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import tempfile
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.memory.facts import Fact
from agent.brain.runtime.memory.memory_search import MemorySearch
from agent.brain.runtime.memory.vector_store import VectorStore


class FakeEmbedder:
    """把「同義詞」映到同一個方向：小說/小説/ラノベ/novel 一組、貓/猫/neko 一組。"""

    model_name = "fake-e5"
    GROUPS = {
        "novel": ("小説", "小说", "小說", "ラノベ", "ライトノベル", "novel", "book",
                  "書籍", "那本書", "那本书"),
        "cat": ("猫", "貓", "ねこ", "ネコ", "cat", "neko"),
        "tea": ("茶", "お茶", "tea", "ほうじ茶"),
    }

    def embed(self, texts, *, kind: str = "passage"):
        out = []
        for text in texts:
            body = str(text or "")
            vec = [0.0, 0.0, 0.0]
            for index, words in enumerate(self.GROUPS.values()):
                if any(word in body for word in words):
                    vec[index] = 1.0
            if not any(vec):
                vec = [0.0, 0.0, 0.0]
            norm = sum(value * value for value in vec) ** 0.5 or 1.0
            out.append([value / norm for value in vec])
        return out

    def snapshot(self):
        return {"model": self.model_name, "available": True}


class FakeMemory:
    """用真的 `SqliteMemory`（schema 與生產一致），只是不落種子。"""


class TestHybridSearch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        from agent.adapter.memory_sqlite import SqliteMemory
        self.memory = SqliteMemory(self.root / "facts.db", seed=False,
                                   entity_ids=("mira", "aster", "nori"))
        self.memory.add("主人はラノベを読んでいる（佐々木とピーちゃん）", layer="pair",
                        audience={"mira", "owner"}, principal="owner", entity="mira",
                        source="test")
        self.memory.add("主人の家の猫の名前はノブ", layer="pair",
                        audience={"mira", "owner"}, principal="owner", entity="mira",
                        source="test")
        self.memory.add("主人はほうじ茶が好き", layer="private",
                        audience={"mira"}, principal="owner", entity="mira",
                        source="test")
        self.db = self.memory.path
        self.vectors = VectorStore(self.root / "vectors.db", embedder=FakeEmbedder())
        self.search = MemorySearch(memory=self.memory, db_path=self.db, vectors=self.vectors)
        # 查詢路徑只讀快取 ⇒ 測試要先建索引（生產由開機暖機做）
        self.search.warmup(("mira",))

    def hit_texts(self, query, *, present=("mira", "owner"), layers=("fact",)):
        out = self.search.search(entity="mira", present=set(present), query=query,
                                 limit=5, layers=layers)
        return [row["text"] for row in out["facts"]]

    def test_semantic_hit_across_wording(self):
        # 字面 0 重疊（小説 vs ラノベ），但語意同組 ⇒ 必須命中
        for query in ("小説", "小说", "novel", "那本書"):
            hits = self.hit_texts(query)
            self.assertTrue(any("ラノベ" in text for text in hits),
                            f"{query} 應該找到ラノベ那條，實際={hits}")

    def test_unrelated_query_returns_nothing(self):
        hits = self.hit_texts("今日は雨だ")
        self.assertEqual(hits, [], f"無關的句子不該回東西，實際={hits}")

    def test_disclosure_not_bypassed_by_semantics(self):
        # ほうじ茶那條的房間只有 {mira}：owner 在場時就不該回
        hits = self.hit_texts("お茶", present=("mira", "owner"))
        self.assertEqual([text for text in hits if "ほうじ茶" in text], [],
                         f"披露規則被語意繞過了：{hits}")

    def test_lexical_only_mode_still_works(self):
        plain = MemorySearch(memory=self.memory, db_path=self.db, vectors=None)
        out = plain.search(entity="mira", present={"mira", "owner"},
                           query="猫の名前", limit=5, layers=("fact",))
        self.assertTrue(any("ノブ" in row["text"] for row in out["facts"]))

    def test_vector_store_is_idempotent(self):
        items = [("ref-1", "主人はラノベを読んでいる")]
        first = self.vectors.sync("fact", "mira", items)
        second = self.vectors.sync("fact", "mira", items)
        self.assertEqual(first, 1)
        self.assertEqual(second, 0, "文本沒變不該重嵌")

    def test_parts_are_reported(self):
        out = self.search.search(entity="mira", present={"mira", "owner"},
                                 query="ラノベ", limit=3, layers=("fact",))
        top = out["facts"][0]
        self.assertIn("parts", top)
        self.assertGreater(top["parts"]["semantic"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
