#!/usr/bin/env python3
"""AttentionGate 的四级动作、租约、延迟补位。"""

from __future__ import annotations

from demo_common import *  # noqa: F401,F403
from agent.brain.domain.attention import AttentionConfig


class TestAttention(unittest.TestCase):
    def test_other_principal_is_silent(self):
        state = make_state()
        result = state.send("Bob，你昨天说的那个呢？", entity="mira",
                            principal="alice", group=True, addressed_to="bob",
                            participants=["owner", "alice", "bob"])
        self.assertEqual(result["decision"]["engage"], "silent")
        self.assertTrue(result["observed"])

    def test_open_question_with_memory_yields(self):
        state = make_state(AttentionConfig(yield_ms=120))
        result = state.send("周末回老家吗？", entity="mira", principal="alice",
                            group=True, participants=["owner", "alice"])
        self.assertTrue(result["yielded"])
        self.assertEqual(result["decision"]["engage"], "yield")
        self.assertEqual(result["decision"]["yield_ms"], 120)
        self.assertTrue(result["state"]["pending"])

    def test_yield_delivers_after_window(self):
        state = make_state(AttentionConfig(yield_ms=100))
        state.send("周末回老家吗？", entity="mira", principal="alice",
                   group=True, participants=["owner", "alice"])
        self.assertTrue(wait_for(lambda: not state.snapshot()["pending"], 2.0))
        thread = next(t for t in state.snapshot()["threads"]
                      if t["scope"] == "group-discord-lobby")
        self.assertTrue(any(message["role"] == "assistant"
                            for message in thread["messages"]))

    def test_new_human_message_cancels_yield(self):
        state = make_state(AttentionConfig(yield_ms=600))
        state.send("周末回老家吗？", entity="mira", principal="alice",
                   group=True, participants=["owner", "alice"])
        self.assertTrue(state.snapshot()["pending"])
        state.send("先不用了，谢谢", entity="mira", principal="alice",
                   group=True, participants=["owner", "alice"])
        self.assertFalse(state.snapshot()["pending"])

    def test_memo_gets_reaction(self):
        state = make_state()
        result = state.send("大家注意下，明天下午三点跟客户对齐", entity="mira",
                            principal="alice", group=True,
                            participants=["owner", "alice"])
        self.assertEqual(result["decision"]["engage"], "react")
        self.assertTrue(any(frame["kind"] == "reaction" for frame in result["frames"]))

    def test_focus_lease_allows_follow_up_without_mention(self):
        state = make_state()
        first = state.send("ミラ，你觉得呢？", entity="mira", principal="alice",
                           group=True, mention=True, participants=["owner", "alice"])
        self.assertEqual(first["decision"]["engage"], "reply")
        follow = state.send("那如果换成 Redis 呢？", entity="mira",
                            principal="alice", group=True,
                            participants=["owner", "alice"])
        self.assertEqual(follow["decision"]["engage"], "reply")
        self.assertEqual(follow["decision"]["engage_reason"], "focus_lease")

    def test_terminator_revokes_lease(self):
        state = make_state()
        state.send("ミラ，你觉得呢？", entity="mira", principal="alice",
                   group=True, mention=True, participants=["owner", "alice"])
        done = state.send("好的谢谢", entity="mira", principal="alice",
                          group=True, participants=["owner", "alice"])
        self.assertEqual(done["decision"]["engage"], "silent")

    def test_unknown_addressee_blocks_focus_lease(self):
        """群聊裡「查過、不是對她說」⇒ 直接旁聽，FocusLease 不能把她叫回來。

        2026-10-27 實測：三顆 bot 各自因為先前被叫到過而持有一份租約，
        一句沒指名的「今日何をしました？」三顆都回答了 ⇒ 租約不該越過收件人判定。
        """
        state = make_state()
        first = state.send("ミラ，你觉得呢？", entity="mira", principal="alice",
                           group=True, mention=True, participants=["owner", "alice"])
        self.assertEqual(first["decision"]["engage"], "reply")
        follow = state.send("今日何をしました？", entity="mira", principal="alice",
                            group=True, addressed_to="unknown",
                            participants=["owner", "alice"])
        self.assertEqual(follow["decision"]["engage"], "silent")
        self.assertTrue(follow["observed"])
        self.assertEqual(follow["decision"]["engage_reason"], "addressee_unknown")

    def test_entity_addressee_still_replies_in_group(self):
        state = make_state()
        result = state.send("今日何をしました？", entity="mira", principal="alice",
                            group=True, addressed_to="entity",
                            participants=["owner", "alice"])
        self.assertEqual(result["decision"]["engage"], "reply")


if __name__ == "__main__":
    unittest.main(verbosity=2)
