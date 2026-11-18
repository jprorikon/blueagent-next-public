#!/usr/bin/env python3
"""持久化：事实、线程、片段在重启后仍然在；reset 只清对话，不丢种子。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from demo_common import *  # noqa: F401,F403
from agent.brain.memory.fact_store import FactStore
from agent.brain.memory.facts import Fact
from agent.brain.domain.ids import ThreadKey


def owner_key(entity: str = "mira") -> ThreadKey:
    return ThreadKey(scope="owner", entity=entity, scene="together", purpose="chat")


class TestPersistence(unittest.TestCase):
    def test_memory_and_thread_survive_restart(self):
        root = tempfile.mkdtemp(prefix="ba-persist-")
        first = AppState(build(brain_delay=0, data_dir=root,
                                step_duration=0.02, life_pause=0.005))
        try:
            result = first.send("记住：我最喜欢的颜色是蓝色",
                                entity="mira", principal="owner")
            self.assertTrue(result["ok"], result.get("error"))
            thread = first.app.threads.current_for(owner_key())
            self.assertIsNotNone(thread)
            thread_id = thread.id
            self.assertGreaterEqual(thread.turns, 1)
        finally:
            first.shutdown()

        second = AppState(build(brain_delay=0, data_dir=root,
                                 step_duration=0.02, life_pause=0.005))
        try:
            facts = second.app.memory.snapshot({"owner"})["facts"]
            self.assertTrue(any("蓝色" in fact["text"] for fact in facts))
            thread = second.app.threads.current_for(owner_key())
            self.assertIsNotNone(thread)
            self.assertEqual(thread.id, thread_id)
            self.assertGreaterEqual(thread.turns, 1)
            self.assertTrue(any("蓝色" in turn.text for turn in thread.transcript))
        finally:
            second.shutdown()

    def test_episode_survives_restart(self):
        root = tempfile.mkdtemp(prefix="ba-persist-ep-")
        first = AppState(build(brain_delay=0, data_dir=root,
                                step_duration=0.02, life_pause=0.005))
        try:
            first.send("第一句", entity="mira", principal="owner")
            first.send("第二句", entity="mira", principal="owner")
            sealed = first.seal(entity="mira", principal="owner")
            self.assertTrue(sealed["ok"])
            episode_id = sealed["sealed"]["id"]
            self.assertEqual(len(first.app.archive.all()), 1)
        finally:
            first.shutdown()

        second = AppState(build(brain_delay=0, data_dir=root,
                                 step_duration=0.02, life_pause=0.005))
        try:
            episodes = second.app.archive.all()
            self.assertEqual([episode.id for episode in episodes], [episode_id])
            self.assertGreaterEqual(len(episodes[0].turns), 2)
            self.assertEqual(episodes[0].turns[0].speaker, "owner")
        finally:
            second.shutdown()

    def test_reset_clears_dialogue_and_facts(self):
        # 生產的 reset 重建時不播 demo 種子（種子只在測試的 demo_people 裡）⇒ 清完就是空的
        root = tempfile.mkdtemp(prefix="ba-reset-")
        state = AppState(build(brain_delay=0, data_dir=root,
                                step_duration=0.02, life_pause=0.005))
        try:
            state.send("记住：我最喜欢的颜色是蓝色",
                       entity="mira", principal="owner")
            state.seal(entity="mira", principal="owner")
            self.assertTrue(any("蓝色" in fact["text"]
                                for fact in state.app.memory.snapshot({"owner"})["facts"]))
            self.assertEqual(len(state.app.threads), 1)
            self.assertEqual(len(state.app.archive.all()), 1)

            state.reset()
            facts = state.app.memory.snapshot({"owner"})["facts"]
            self.assertFalse(any("蓝色" in fact["text"] for fact in facts))
            self.assertEqual(len(state.app.threads), 0)
            self.assertEqual(len(state.app.archive.all()), 0)
            self.assertFalse(any(fact["source"] == "seed" for fact in facts))
        finally:
            state.shutdown()

    def test_corrupt_fact_line_is_skipped(self):
        root = tempfile.mkdtemp(prefix="ba-fact-")
        store = FactStore(Path(root) / "facts.jsonl")
        store.append(Fact("好事实", layer="world", source="manual"))
        with store.path.open("a", encoding="utf-8") as stream:
            stream.write("{broken json\\n")
        facts = store.load()
        self.assertEqual([fact.text for fact in facts], ["好事实"])
        self.assertEqual(json.loads(store.path.read_text().splitlines()[0])["text"],
                         "好事实")


if __name__ == "__main__":
    unittest.main(verbosity=2)
