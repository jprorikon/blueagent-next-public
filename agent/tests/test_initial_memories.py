#!/usr/bin/env python3
"""前史（初期記憶）與她的関心——兩層都要能被驗收。

判據：
  ① 関心檔的**讀取規則只有一條**：見出しに「興味」を含み「相手」を含まない節＝自分側、
     相手を含む節＝接話用。両方が prompt の違う節として出る（混ざったら相手の趣味を調べに行く）。
  ② 関心檔が無い角色は**零回歸**（空字串、例外を投げない）。
  ③ 自主任務の persona にその関心が**実際に載る**（載っていないと、彼女は自分の興味で調べない）。
  ④ 前史は `days_ago` を**イベント時間**（valid_from）に落とし、既定は dry-run、
     既に入っている文は**飛ばす**（何度走らせても増えない）。
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent" / "tools"))

from agent.brain.persona.interests import InterestsLibrary  # noqa: E402
import seed_initial_memories as seed  # noqa: E402

SAMPLE = """# テストの関心

## 彼女自身の興味

- 爆弾の収集
- 音楽

## 相手（オーナー）の興味

- 仕事の話
"""


class TestInterests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="ba-interests-"))
        (self.root / "mira.interests.md").write_text(SAMPLE, encoding="utf-8")

    def test_own_and_other_are_split_by_heading(self):
        library = InterestsLibrary(self.root)
        self.assertEqual(library.own("mira"), ["爆弾の収集", "音楽"])
        self.assertEqual(library.other("mira"), ["仕事の話"])

    def test_block_keeps_the_two_sides_apart(self):
        block = InterestsLibrary(self.root).block("mira")
        self.assertIn("自分から調べに行く側", block)
        self.assertIn("自分からは調べない", block)
        self.assertLess(block.index("爆弾の収集"), block.index("仕事の話"))

    def test_missing_file_is_a_no_op(self):
        library = InterestsLibrary(self.root)
        self.assertEqual(library.block("aster"), "")
        self.assertEqual(library.own("aster"), [])
        self.assertEqual(InterestsLibrary(None).block("mira"), "")

    def test_ships_for_the_three_characters(self):
        library = InterestsLibrary(REPO / "agent" / "persona")
        for entity in ("mira", "nori", "aster"):
            with self.subTest(entity=entity):
                own = library.own(entity)
                self.assertTrue(own, f"{entity} の関心檔が読めない")
                self.assertIn("自分からは調べない", library.block(entity))


class TestSelfWorkPersona(unittest.TestCase):
    """自主任務の persona に関心が載るか（載らないと彼女は自分の興味で調べない）。"""

    def test_interest_block_reaches_the_step_prompt(self):
        from agent.brain.compose import life_wiring
        from agent.brain.persona.soul import SoulLibrary
        import agent.brain.runtime.self_work as self_work

        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-selfwork-"))
        (root / "SOUL.md").write_text("# SOUL\n常に日本語。", encoding="utf-8")
        (root / "mira.md").write_text("ミラ。一人称は「私」。", encoding="utf-8")
        (root / "mira.interests.md").write_text(SAMPLE, encoding="utf-8")

        captured: dict = {}
        original = self_work.run_step

        def fake_run_step(*args, **kwargs):
            captured.update(kwargs)
            return {"ok": True, "text": "できた", "trace": [], "rounds": 1}

        self_work.run_step = fake_run_step
        try:
            work = life_wiring.make_self_work(brain=object(), tools=object(),
                                              souls=SoulLibrary(root), speak_lang="ja",
                                              interests=InterestsLibrary(root))
            work("調べる", "読む", entity="mira")
        finally:
            self_work.run_step = original
        persona = str(captured.get("persona") or "")
        self.assertIn("ミラ", persona)
        self.assertIn("爆弾の収集", persona)


class TestSeedPlan(unittest.TestCase):
    def setUp(self):
        self.data = pathlib.Path(tempfile.mkdtemp(prefix="ba-seed-"))
        (self.data / "persona").mkdir(parents=True, exist_ok=True)
        (self.data / "memory").mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "events": [
            {"text": "昔の出来事", "days_ago": 100},
            {"text": "もっと前の出来事", "days_ago": 400, "kind": "event", "layer": "self"},
        ]}
        (self.data / "persona" / "mira.initial_memories.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_days_ago_becomes_event_time(self):
        rows = seed.plan(data=self.data, entities=("mira",), now=10_000_000.0)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["valid_from"], 10_000_000.0 - 100 * 86400)
        self.assertAlmostEqual(rows[1]["valid_from"], 10_000_000.0 - 400 * 86400)
        self.assertTrue(all(not row["skip"] for row in rows))

    def test_already_stored_text_is_skipped(self):
        from agent.brain.memory.facts import normalize_text
        import sqlite3
        db = sqlite3.connect(str(self.data / "memory" / "facts.db"))
        db.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, text TEXT, norm TEXT, entity TEXT)")
        db.execute("INSERT INTO facts (text, norm, entity) VALUES (?,?,?)",
                   ("昔の出来事", normalize_text("昔の出来事"), "mira"))
        db.commit()
        db.close()
        rows = seed.plan(data=self.data, entities=("mira",), now=10_000_000.0)
        marks = {row["text"]: row["skip"] for row in rows}
        self.assertTrue(marks["昔の出来事"])
        self.assertFalse(marks["もっと前の出来事"])

    def test_dry_run_writes_nothing(self):
        before = sorted(p.name for p in (self.data / "memory").iterdir())
        code = seed.main(["--data", str(self.data), "--entity", "mira"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in (self.data / "memory").iterdir()), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
