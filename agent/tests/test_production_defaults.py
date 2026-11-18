#!/usr/bin/env python3
"""生產的初值不能有 demo 資料（2026-10-25 第 1 階段）。

判據：
  ① 生產 `build()`（不經測試的 demo_people）只有主人：沒有 alice／bob、沒有預設關係、
     沒有對他們的立場、沒有種子記憶；
  ② 名字跟著註冊表走：之後才註冊的客人也有顯示名（摘要與記憶都用它）；
  ③ 全新的空 data/ 也開得起來（以前 `memory/` 目錄是 demo 種子順手建的）；
  ④ 生產原始碼裡沒有 `"alice"`／`"bob"` 這種字面值（假人物只准住在 tests/）。
"""

from __future__ import annotations

import ast
import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.bootstrap import build as build_production
from agent.brain.compose.summary import summarize
from agent.brain.domain.episode import CutReason, Episode
from agent.brain.domain.principal import Principal
from agent.brain.domain.thread import ThreadKey, TurnRecord

ROOT = pathlib.Path(__file__).resolve().parents[2]


class TestProductionBuildHasNoDemoWorld(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 全新空目錄：順便驗 ③（以前在這裡 `vectors.db` 開不起來）
        cls.root = pathlib.Path(tempfile.mkdtemp(prefix="ba-prod-"))
        cls.app = build_production(brain_delay=0, data_dir=cls.root,
                                    step_duration=0.02, life_pause=0.005)

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown_lives()

    def test_only_the_owner_is_registered(self):
        self.assertEqual([item.id for item in self.app.principals.all()], ["owner"])

    def test_no_preset_relations(self):
        snapshot = self.app.social.snapshot() if hasattr(self.app.social, "snapshot") else {}
        for key in ("relations", "circles"):
            self.assertFalse((snapshot or {}).get(key), snapshot)

    def test_stances_only_for_the_owner(self):
        for entity in ("mira", "aster", "nori"):
            people = {row.get("principal") for row in
                      self.app.stances.snapshot(entity, names={})}
            self.assertEqual(people, {"owner"}, (entity, people))

    def test_memory_starts_empty(self):
        """生產初值**沒有種子記憶**（沒有 alice／bob、沒有預設關係）。

        她自己的生活事實（`layer='self'`）不算——那是她自己做出來的，不是 demo 種子。
        （2026-11-03：生活步進換成離線替身之後，她真的會在測試窗口裡走完幾步。）
        """
        facts = self.app.memory.snapshot()["facts"]
        seeded = [row for row in facts if str(row.get("source") or "") in {"seed", "demo"}]
        self.assertEqual(seeded, [], seeded)
        people = [row for row in facts
                  if str(row.get("layer") or "") in {"world", "pair", "circle", "private"}]
        self.assertEqual(people, [], people)

    def test_fresh_data_dir_has_memory_folder(self):
        self.assertTrue((self.root / "memory" / "vectors.db").exists()
                        or getattr(self.app, "vectors", None) is None)


class TestNamesFollowTheRegistry(unittest.TestCase):
    def test_guest_registered_later_has_a_name(self):
        demo = build_production(brain_delay=0, data_dir=tempfile.mkdtemp(prefix="ba-names-"),
                                step_duration=0.02, life_pause=0.005)
        self.addCleanup(demo.shutdown_lives)
        names = demo.principals.names()
        self.assertEqual(names.get("owner"), "主人")
        self.assertIsNone(names.get("carol"))
        demo.principals.register(Principal("carol", "キャロル", role="guest"))
        self.assertEqual(names.get("carol"), "キャロル", "活視圖：核可後就查得到")
        episode = Episode(id="ep", key=ThreadKey(scope="carol", entity="mira"),
                          thread_id="th", index=0, reason=CutReason.TURNS,
                          started_at=0, ended_at=1,
                          turns=(TurnRecord(turn_id="t1", role="user", text="こんにちは",
                                            source="app", at=0, speaker="carol"),))
        self.assertIn("キャロル", summarize(episode, names))


class TestNoDemoPeopleInProductionSource(unittest.TestCase):
    def test_no_alice_or_bob_literals(self):
        hits = []
        for folder in ("brain", "adapter", "server"):
            for path in (ROOT / "agent" / folder).rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                            and node.value.strip().lower() in {"alice", "bob"}):
                        hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(hits, [], "假人物只准住在 tests/demo_people.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)
