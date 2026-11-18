"""語音出口發 `motion` 幀的接線驗收（計畫 §5.4）：動作要在她開口**之前**發一條，而且只發一次。

這裡只測「我加的那一段接線」：真的 `MotionLibrary`（合成片段）＋ 假的 voice／env／ctx，
不跑整個 App。整條 AppState 的端點語意在 `tests/test_motion.py` 與 `tools/_probe_motion_turn.sh`。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from agent.brain.compose.motion import MotionPlanner                  # noqa: E402
from agent.brain.compose.stages.brain import _Motion                  # noqa: E402
from agent.brain.compose.stages.context import TurnContext            # noqa: E402
from agent.brain.compose.stages.egress import VoiceEgressStage       # noqa: E402
from agent.brain.media.motion import MotionLibrary               # noqa: E402
from test_motion import write_clip, make_clip                 # noqa: E402


class FakeCtx:
    def __init__(self) -> None:
        self.frames: list[tuple[str, dict]] = []

    def emit(self, kind: str, payload: dict) -> None:
        self.frames.append((str(kind), dict(payload)))


class FakeVoice:
    """只回一幀 speech；記下被要求唸什麼。"""

    def __init__(self) -> None:
        self.spoken: list[str] = []

    def maybe_speak(self, entity, reply, **kwargs):
        self.spoken.append(reply)
        return [{"kind": "speech", "payload": {"text": reply}}]

    def release_floor(self, *args, **kwargs) -> None:
        return None


def make_env(reply: str, source: str = "phone", spoken: str = ""):
    return SimpleNamespace(
        meta={"reply_text": reply, "spoken_text": spoken, "voice_target": "auto"},
        decision=SimpleNamespace(entity="mira", principal="owner"),
        thread=SimpleNamespace(id="th_test-1"),
        event=SimpleNamespace(source=source),
    )


class TestVoiceEgressEmitsMotion(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        write_clip(self.root, make_clip(tag="greet", frames=90))
        self.library = MotionLibrary(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _stage(self, *, enabled: bool = True) -> tuple[VoiceEgressStage, FakeVoice]:
        voice = FakeVoice()
        return VoiceEgressStage(voice, motion=MotionPlanner(self.library, enabled=enabled)), voice

    def test_motion_frame_comes_before_speech(self):
        stage, voice = self._stage()
        ctx = FakeCtx()
        stage.run(make_env("おはよう、オーナー"), ctx)
        kinds = [kind for kind, _ in ctx.frames]
        self.assertEqual(kinds, ["motion", "speech"])
        self.assertEqual(ctx.frames[0][1]["tag"], "greet")
        self.assertTrue(ctx.frames[0][1]["url"].startswith("/api/motion/"))
        self.assertEqual(voice.spoken, ["おはよう、オーナー"])       # 話照講

    def test_motion_still_fires_when_reply_was_already_spoken_while_streaming(self):
        """首句優先那條路會在串流時把整段唸掉（`spoken_text` 蓋滿）⇒ 語音出口會 early-return。

        2026-10-31 實機「嘴動、身體不動」就是踩在這：動作發射必須在 early-return 之前。
        """
        stage, voice = self._stage()
        ctx = FakeCtx()
        stage.run(make_env("おはよう、オーナー。今日は早いね", spoken="おはよう、オーナー。今日は早いね"), ctx)
        kinds = [kind for kind, _ in ctx.frames]
        self.assertEqual(kinds, ["motion"])                        # 有動作、不重複合成
        self.assertEqual(voice.spoken, [])                         # 沒再唸一次

    def test_no_keyword_means_no_motion_frame(self):
        stage, _ = self._stage()
        ctx = FakeCtx()
        stage.run(make_env("今日は静かな一日だった"), ctx)
        self.assertEqual([kind for kind, _ in ctx.frames], ["speech"])

    def test_discord_turn_gets_no_motion(self):
        stage, _ = self._stage()
        ctx = FakeCtx()
        stage.run(make_env("おはよう", source="discord:vc"), ctx)
        self.assertEqual([kind for kind, _ in ctx.frames], ["speech"])

    def test_disabled_planner_emits_nothing(self):
        stage, _ = self._stage(enabled=False)
        ctx = FakeCtx()
        stage.run(make_env("おはよう"), ctx)
        self.assertEqual([kind for kind, _ in ctx.frames], ["speech"])

    def test_empty_library_fails_open(self):
        voice = FakeVoice()
        stage = VoiceEgressStage(voice, motion=MotionPlanner(MotionLibrary(self.root / "empty")))
        ctx = FakeCtx()
        stage.run(make_env("おはよう"), ctx)
        self.assertEqual([kind for kind, _ in ctx.frames], ["speech"])

    def test_broken_planner_does_not_break_the_turn(self):
        class Boom:
            def frame(self, **kwargs):
                raise RuntimeError("boom")

        voice = FakeVoice()
        stage = VoiceEgressStage(voice, motion=Boom())
        ctx = FakeCtx()
        stage.run(make_env("おはよう"), ctx)                      # 不能拋
        self.assertEqual([kind for kind, _ in ctx.frames], ["speech"])

    def test_egress_does_not_repeat_what_brain_already_sent(self):
        stage, voice = self._stage()
        ctx = FakeCtx()
        env = make_env("おはよう、オーナー")
        env.meta["motion_sent"] = True                             # brain 開口時已經發過
        stage.run(env, ctx)
        self.assertEqual([kind for kind, _ in ctx.frames], ["speech"])


class TestBrainEmitsMotionWhenSheStartsTalking(unittest.TestCase):
    """動作要跟著**第一個命中的完整句子**發，不是等整輪講完（2026-10-31 實測：她先講完才動）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        write_clip(self.root, make_clip(tag="greet", frames=90))
        write_clip(self.root, make_clip(tag="laugh", frames=60))
        self.planner = MotionPlanner(MotionLibrary(self.root))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _feed(self, pieces, *, source="phone", planner=None):
        env = make_env("", source=source)
        motion = _Motion(planner or self.planner, env, TurnContext.of(env))
        ctx = FakeCtx()
        seen = []                                                  # 每塊 delta 之後已經發了幾幀
        for piece in pieces:
            motion.feed(ctx, piece)
            seen.append(len(ctx.frames))
        return motion, ctx, env, seen

    def test_fires_as_soon_as_the_first_sentence_is_complete(self):
        _, ctx, env, seen = self._feed(["おは", "よ、オーナー", "。", "今日は", "早いね。"])
        self.assertEqual(seen, [0, 0, 1, 1, 1])                    # 第一句收尾那一刻就發
        self.assertEqual(ctx.frames[0][0], "motion")
        self.assertEqual(ctx.frames[0][1]["tag"], "greet")
        self.assertTrue(env.meta["motion_sent"])

    def test_half_sentence_does_not_fire(self):
        _, ctx, _, _ = self._feed(["おはよ、せんせ"])
        self.assertEqual(ctx.frames, [])

    def test_only_one_motion_per_turn(self):
        _, ctx, _, _ = self._feed(["おはよう。", "うふふ、", "あはは。"])
        self.assertEqual([frame[1]["tag"] for frame in ctx.frames], ["greet"])

    def test_later_sentence_can_still_fire(self):
        _, ctx, _, seen = self._feed(["今日は静かだね。", "うふふ、なんでもない。"])
        self.assertEqual(seen, [0, 1])
        self.assertEqual(ctx.frames[0][1]["tag"], "laugh")

    def test_finish_falls_back_to_the_whole_reply(self):
        motion, ctx, _, _ = self._feed(["うふふ"])                 # 沒有句末標點 ⇒ 串流時不發
        self.assertEqual(ctx.frames, [])
        motion.finish(ctx, "うふふ")
        self.assertEqual([frame[1]["tag"] for frame in ctx.frames], ["laugh"])

    def test_non_stage_source_marks_handled_without_frame(self):
        motion, ctx, env, _ = self._feed(["おはよう。", "うふふ。"], source="discord:vc")
        motion.finish(ctx, "おはよう。うふふ。")
        self.assertEqual(ctx.frames, [])
        self.assertTrue(env.meta["motion_sent"])                   # 語音出口也不會再試

    def test_no_planner_is_a_no_op(self):
        env = make_env("")
        motion = _Motion(None, env, TurnContext.of(env))
        ctx = FakeCtx()
        motion.feed(ctx, "おはよう。")
        motion.finish(ctx, "おはよう。")
        self.assertEqual(ctx.frames, [])


class TestModelChosenMotion(unittest.TestCase):
    """她自己在回覆開頭標 `[[motion:tag]]`（owner 2026-10-31）：抽走不唸不顯示、優先於規則表。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        write_clip(self.root, make_clip(tag="greet", frames=90))
        write_clip(self.root, make_clip(tag="cheer", frames=60))
        self.planner = MotionPlanner(MotionLibrary(self.root))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_filter_strips_tag_even_when_split(self):
        from agent.brain.compose.stages.brain import _TagFilter
        got: list[str] = []
        flt = _TagFilter(got.append)
        pieces = ["[[mot", "ion:che", "er]]やった", "ね！ [a] と [[x]]", "。"]
        text = "".join(flt.feed(piece) for piece in pieces) + flt.flush()
        self.assertEqual(text, "やったね！ [a] と [[x]]。")
        self.assertEqual(got, ["cheer"])

    def test_filter_releases_a_lone_bracket_at_the_end(self):
        from agent.brain.compose.stages.brain import _TagFilter
        flt = _TagFilter(lambda tag: None)
        self.assertEqual(flt.feed("配列は a["), "配列は a")
        self.assertEqual(flt.flush(), "[")

    def test_chosen_tag_beats_rules(self):
        env = make_env("")
        motion = _Motion(self.planner, env, TurnContext.of(env))
        ctx = FakeCtx()
        motion.chosen(ctx, "cheer")
        motion.feed(ctx, "おはよう、オーナー。")               # 規則會挑 greet，但已經標過了
        self.assertEqual([frame[1]["tag"] for frame in ctx.frames], ["cheer"])

    def test_tags_offered_only_on_stage(self):
        self.assertEqual(self.planner.tags("phone:owner"), ["greet", "cheer"])
        self.assertEqual(self.planner.tags("discord:owner"), [])
        self.assertEqual(self.planner.tags("phone:call"), [])

    def test_clean_reply_drops_the_tag(self):
        from agent.brain.runtime.text_clean import clean_reply
        self.assertEqual(clean_reply("[[motion:greet]]おはよう"), "おはよう")

    def test_prompt_block_lists_tags_only_when_offered(self):
        from agent.brain.messages import build_messages
        from agent.brain.ports import BrainRequest

        # 2026-11-02 起本輪段（含動作清單）併進最後那條用戶訊息，不再是單獨的 system ⇒ 看整份 prompt
        def prompt_text(tags):
            request = BrainRequest(thread_id="t", entity="mira", principal="owner", text="やあ",
                                   system="あなたはミラ。", mode="chat", want_voice=False,
                                   deadline_ms=0, meta={"motion_tags": tags})
            return "\n".join(m["content"] for m in build_messages(request))

        self.assertIn("[[motion:名前]]", prompt_text(["greet", "cheer"]))
        self.assertIn("greet、cheer", prompt_text(["greet", "cheer"]))
        self.assertNotIn("[[motion]]", prompt_text([]))


if __name__ == "__main__":
    unittest.main()
