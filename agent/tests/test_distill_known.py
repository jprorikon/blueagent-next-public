"""提煉時給模型看「已經記得的相關事實」（PENDING B.Agent.3：多值事實的同義碎片，2026-11-17）。

判據：
  ① 有向量：跨語言的同一件事（窗口日文、舊事實中文）也挑得出來，無關的不挑；
  ② 沒向量：退回二字重疊；
  ③ 只挑仍有效、同場景、不帶槽位的；
  ④ 真的跑一次提煉：送給模型的 prompt 裡有這一段，位置在「会話ウィンドウ：」之前；
  ⑤ 嵌入服務掛了不擋提煉（退回字面）。
"""

from __future__ import annotations

import json

from test_distill import DistillCase

from agent.brain.runtime.memory.distill_known import known_hint, related_known

PAIR = {"mira", "owner"}
COLD_ZH = "主人怕冷，空调一般开在 26 度。"
NOVEL = "主人は『佐々木とピーちゃん』を読んでいる。"
TEA = "主人は紅茶より緑茶が好き。"


class FakeVectors:
    """`VectorStore` 的最小替身：事實向量照 ref（正文）存；查詢文含「寒」就靠近怕冷那條。"""

    def __init__(self, facts: dict[str, list[float]], *, broken: bool = False):
        self.facts = facts
        self.broken = broken
        self.embedder = self

    def embed(self, texts, kind="passage"):
        if self.broken:
            raise OSError("embed down")
        text = texts[0]
        return [[1.0, 0.0, 0.0] if "寒" in text else [0.0, 0.0, 1.0]]

    def cached_only(self, kind, entity):
        return dict(self.facts)


class KnownCase(DistillCase):
    def add(self, text, *, slot="", game=0):
        return self.memory.add(text, layer="pair", entity="mira", audience=PAIR, source="distill",
                               kind="state" if slot else "preference", slot=slot, game=game)

    def test_vectors_find_the_same_thing_across_languages(self):
        for text in (COLD_ZH, NOVEL, TEA):
            self.add(text)
        vectors = FakeVectors({COLD_ZH: [0.98, 0.1, 0.0], NOVEL: [0.0, 1.0, 0.0], TEA: [0.1, 0.0, 0.2]})
        found = related_known(self.memory, "mira", 0, "主人は寒がりで、エアコンは26度にしている", vectors=vectors)
        self.assertEqual(found, [COLD_ZH], "字面幾乎不重疊，只有向量挑得到")

    def test_without_vectors_falls_back_to_bigrams(self):
        self.add(NOVEL)
        self.add(TEA)
        found = related_known(self.memory, "mira", 0, "佐々木とピーちゃんの七巻が出たね")
        self.assertEqual(found, [NOVEL])

    def test_only_active_scene_matched_and_slotless(self):
        self.add("主人は佐々木とピーちゃんを六巻まで読んだ", slot="主人がいま読んでいる本")
        self.add("主人は佐々木とピーちゃんの騎士を演じている", game=1)
        self.assertEqual(related_known(self.memory, "mira", 0, "佐々木とピーちゃんを読んだ"), [])

    def test_embed_failure_does_not_block(self):
        self.add(NOVEL)
        found = related_known(self.memory, "mira", 0, "佐々木とピーちゃんの話",
                              vectors=FakeVectors({}, broken=True))
        self.assertEqual(found, [NOVEL])

    def test_hint_text(self):
        self.assertEqual(known_hint([]), "")
        hint = known_hint([COLD_ZH])
        self.assertIn(COLD_ZH, hint)
        self.assertIn("言語を変えて", hint)

    def test_real_distill_run_shows_the_hint_before_the_window(self):
        self.add(TEA)
        self.seed_session(texts=["緑茶が好きなんだよね、紅茶よりずっと", "そうなんだ"])
        prompts: list[str] = []

        def transport(prompt):
            prompts.append(prompt)
            return json.dumps({"window": {"topic": "お茶", "summary": "お茶の好み"}, "candidates": []},
                              ensure_ascii=False)

        self.distiller(transport).run_once()
        self.assertTrue(prompts, "提煉沒跑")
        prompt = prompts[0]
        self.assertIn("すでに覚えていること", prompt)
        self.assertIn(TEA, prompt)
        self.assertLess(prompt.index(TEA), prompt.rindex("会話ウィンドウ："))


del DistillCase

if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
