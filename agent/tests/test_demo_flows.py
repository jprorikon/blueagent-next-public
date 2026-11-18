#!/usr/bin/env python3
"""Demo：一对一、群聊、记忆隔离、无主源、语音"""

from __future__ import annotations

from demo_common import *  # noqa: F401,F403


class TestDemoFlows(unittest.TestCase):
    def setUp(self) -> None:
        self.state = make_state()

    def test_direct_turn_replies_and_writes_memory(self):
        result = self.state.send("今天有点累", entity="mira", principal="owner")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["decision"]["engage"], "reply")
        self.assertTrue(done_text(result))
        self.assertIn("辛苦", done_text(result))
        thread = next(t for t in result["state"]["threads"] if t["scope"] == "owner")
        self.assertEqual(thread["entity"], "mira")
        self.assertGreaterEqual(len(thread["messages"]), 2)

    def test_group_side_conversation_is_observed_only(self):
        result = self.state.send("Bob，你昨天说的那个企划呢？", entity="mira",
                                 principal="alice", group=True, addressed_to="bob")
        self.assertTrue(result["observed"])
        self.assertEqual(result["decision"]["engage"], "silent")
        self.assertEqual(result["frames"], [])
        thread = next(t for t in result["state"]["threads"]
                      if t["scope"] == "group-discord-lobby")
        last = thread["messages"][-1]
        self.assertEqual(last["speaker"], "alice")
        self.assertEqual(last["addressed_to"], "bob")

    def test_group_mention_replies_to_the_speaker(self):
        result = self.state.send("ミラ，你觉得呢？", entity="mira",
                                 principal="alice", group=True, mention=True)
        self.assertFalse(result["observed"])
        self.assertEqual(result["decision"]["engage"], "reply")
        self.assertEqual(result["decision"]["addressed_to"], "entity")
        self.assertIn("Alice", done_text(result))
        done = next(f for f in result["frames"] if f["kind"] == "done")
        self.assertEqual(done["reply_to"], "alice")
        presence = next(p for p in result["state"]["presence"] if p["entity"] == "mira")
        self.assertEqual(presence["attention_principal"], "alice")

    def test_layered_visibility_by_room(self):
        memory = self.state.app.memory
        scope = MemoryScope("alice", "mira", "together", "th_a-1")

        def texts(query, people):
            speaker = sorted(people)[0] if people else "alice"
            local_scope = MemoryScope(speaker, "mira", "together", f"th_{speaker}-1")
            return [fact.text for fact in memory.recall_detail(
                local_scope, query, limit=6, participants=people)]

        # 家人圈（房間＝她＋主人＋Alice）：
        #   只問「在場的人是不是都在這間房裡」——
        #   主人單獨 ✅、Alice 單獨 ✅（她本來就在那間房）、兩人一起 ✅、
        #   Bob（沒進過這間房）❌、多一個 Bob ❌。
        self.assertTrue(any("回老家" in text for text in texts("周末", {"owner"})))
        self.assertTrue(any("回老家" in text for text in texts("周末", {"alice"})))
        self.assertTrue(any("回老家" in text for text in texts(
            "周末", {"owner", "alice"})))
        self.assertFalse(any("回老家" in text for text in texts("周末", {"bob"})))
        self.assertFalse(any("回老家" in text for text in texts(
            "周末", {"owner", "alice", "bob"})))

        # 同事圈（房間＝她＋主人＋Bob）：主人 / Bob 單獨都可說；Alice 不在那間房 ⇒ 不可說
        self.assertTrue(any("上线" in text for text in texts("上线", {"owner"})))
        self.assertTrue(any("上线" in text for text in texts("上线", {"bob"})))
        self.assertTrue(any("上线" in text for text in texts("上线", {"owner", "bob"})))
        self.assertFalse(any("上线" in text for text in texts("上线", {"alice"})))

        # 私密：只有主人可讀
        self.assertTrue(any("惊喜" in text for text in texts("惊喜", {"owner"})))
        self.assertFalse(any("惊喜" in text for text in texts("惊喜", {"alice"})))
        self.assertFalse(any("惊喜" in text for text in texts("惊喜", {"bob"})))

        # 共識：三人都可讀；主人+Alice 兩人在場也可讀
        # 召回是「二字重疊」：查「吃飯」對不上「吃了飯」，所以這裡用真會重疊的「一起」
        self.assertTrue(any("一起吃了饭" in text for text in texts(
            "一起", {"owner", "alice", "bob"})))
        self.assertTrue(any("一起吃了饭" in text for text in texts(
            "一起", {"owner", "alice"})))

    def test_social_graph_exposes_relations_and_circles(self):
        snapshot = self.state.app.social.snapshot()
        family = next(circle for circle in snapshot["circles"] if circle["id"] == "family")
        work = next(circle for circle in snapshot["circles"] if circle["id"] == "work")
        self.assertEqual(set(family["members"]), {"owner", "alice"})
        self.assertEqual(set(work["members"]), {"owner", "bob"})
        relation = next(item for item in snapshot["relations"]
                        if {item["a"], item["b"]} == {"owner", "alice"})
        self.assertEqual(relation["kind"], "family")

    def test_group_room_roster_changes_recall(self):
        # 三个人都在场：家人记忆不進 prompt
        all_three = self.state.send("周末回老家吗？", entity="mira",
                                    principal="alice", group=True, mention=True,
                                    participants=["owner", "alice", "bob"])
        facts = next((frame["payload"].get("facts", [])
                      for frame in all_three["frames"] if frame["kind"] == "memory"), [])
        self.assertFalse(any("回老家" in text for text in facts))

        # 只有主人 + Alice：家人记忆可讀
        pair = self.state.send("周末回老家吗？", entity="mira",
                               principal="alice", group=True, mention=True,
                               participants=["owner", "alice"])
        facts = next((frame["payload"].get("facts", [])
                      for frame in pair["frames"] if frame["kind"] == "memory"), [])
        self.assertTrue(any("回老家" in text for text in facts))

    def test_private_fact_not_recalled_in_group(self):
        result = self.state.send("生日惊喜是什么？", entity="mira",
                                 principal="owner", group=True, mention=True,
                                 participants=["owner", "alice", "bob"])
        facts = next((frame["payload"].get("facts", [])
                      for frame in result["frames"] if frame["kind"] == "memory"), [])
        self.assertFalse(any("惊喜" in text for text in facts))

    def test_voice_call_is_sticky_and_hangup_unbinds(self):
        called = self.state.send("给我打个 Discord 电话", entity="mira",
                                 principal="owner")
        self.assertTrue(called["ok"], called.get("error"))
        call_frames = [frame for frame in called["frames"] if frame["kind"] == "call"]
        self.assertTrue(call_frames)
        self.assertEqual(call_frames[0]["payload"]["state"], "connected")
        session = called["state"]["voice"]["session"]
        self.assertIsNotNone(session)
        self.assertEqual(session["end"], "discord")
        self.assertTrue(any(frame["kind"] == "speech" for frame in called["frames"]))

        # 通話中的下一輪不需要再說「用語音」，粘性綁定會自動走語音。
        follow = self.state.send("今天怎么样？", entity="mira", principal="owner")
        speech = [frame for frame in follow["frames"] if frame["kind"] == "speech"]
        self.assertTrue(speech)
        self.assertEqual(speech[0]["payload"]["call_id"], session["id"])
        self.assertEqual(speech[0]["payload"]["target"], "discord:vc-lobby")

        ended = self.state.send("挂断", entity="mira", principal="owner")
        self.assertIsNone(ended["state"]["voice"]["session"])
        self.assertFalse(any(frame["kind"] == "speech" for frame in ended["frames"]))

    def test_voice_speak_without_call_uses_current_source(self):
        result = self.state.send("请用语音回我", entity="mira", principal="owner")
        speech = [frame for frame in result["frames"] if frame["kind"] == "speech"]
        self.assertTrue(speech)
        self.assertEqual(speech[0]["payload"]["call_id"], "")
        self.assertTrue(speech[0]["payload"]["target"].startswith("phone:"))

    def test_voice_call_is_idempotent(self):
        first = self.state.send("给我打个 Discord 电话", entity="mira",
                                principal="owner")
        second = self.state.send("再打个 Discord 电话", entity="mira",
                                 principal="owner")
        self.assertEqual(first["state"]["voice"]["session"]["id"],
                         second["state"]["voice"]["session"]["id"])
        self.assertFalse(any(frame["kind"] == "call" for frame in second["frames"]))

    def test_ownerless_source_belongs_to_herself(self):
        result = self.state.send(
            "我推开酒馆的门，看看桌边坐着谁。", entity="mira", ownerless=True,
            participants=["owner", "alice", "bob"])
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["decision"]["principal"], "")
        self.assertEqual(result["decision"]["engage"], "reply")
        thread = next(t for t in result["state"]["threads"] if t["ownerless"])
        self.assertEqual(thread["scope"], "self-trpg-table-1")
        self.assertEqual(result["decision"]["scope"]["self_owned"], True)
        self.assertTrue(result["decision"]["scope"]["private_bank"].endswith("-self"))
        self.assertTrue(any(fact["layer"] == "self"
                            for fact in result["state"]["memory"]["facts"]))

    def test_ownerless_memory_is_visible_regardless_of_room(self):
        memory = self.state.app.memory
        scope = MemoryScope("", "mira", "trpg-table-1", "th_self-1", self_owned=True)
        hits = memory.recall(scope, "灰鸦", limit=5, participants={"owner"})
        self.assertTrue(any("灰鸦" in text for text in hits))

    def test_auto_seal_after_turn_budget(self):
        sealed = None
        for index in range(4):
            result = self.state.send(f"第 {index} 句：今天也辛苦了", entity="mira",
                                     principal="owner")
            if result.get("sealed"):
                sealed = result["sealed"]
                break
        self.assertIsNotNone(sealed, "6 轮后应自动封片")
        self.assertEqual(sealed["reason"], "turns")
        self.assertTrue(sealed["summary"])
        episodes = self.state.snapshot()["episodes"]
        self.assertGreaterEqual(len(episodes), 1)
        self.assertIn("主人", episodes[-1]["summary"])

    def test_seal_endpoint_helper(self):
        self.state.send("随便说一句", entity="mira", principal="owner")
        result = self.state.seal(entity="mira", principal="owner")
        self.assertTrue(result["ok"])
        self.assertEqual(result["sealed"]["reason"], "explicit")


if __name__ == "__main__":
    unittest.main(verbosity=2)
