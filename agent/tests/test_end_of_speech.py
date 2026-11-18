#!/usr/bin/env python3
"""「說完了沒」：判斷器（fail-open）、和糾錯並行、通話裡沒講完的句子扣住併進下一句、Discord 多等一下。

判據：
  ① 判斷器只認 `{"complete": bool}`；端點壞掉 ⇒ None（呼叫端當講完），絕不擋話；
  ② 判斷和 LLM 糾錯並行：總時間 ≈ 較慢的那一個，不是兩個相加；
  ③ 通話：A 沒講完、B 接著到 ⇒ 只有 B 起回合、文字是「A＋B」；沒有 B ⇒ A 等完 `hold_s` 照常送；
  ④ 判不出來（None）或講完 ⇒ 不等；
  ⑤ `/api/audio` 的通話句子會帶 `input_timing`（識別／糾錯／判斷／扣住），寫進 turns.jsonl；
  ⑥ Discord：最後一片沒講完 ⇒ 等待多加 `hold_extra_s`。
"""

from __future__ import annotations

import threading
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.end_judge import EndJudge, EndJudgeConfig, parse_judgement
from agent.adapter.stt_local import LocalSTT, SttConfig, Transcript
from agent.brain.domain.auth import AuthContext, Role
from agent.brain.runtime.voice.speech_hold import SpeechHold
from agent.server import api as api_module

OWNER = AuthContext(principal="owner", role=Role.OWNER, capabilities=frozenset({"chat"}))


class TestJudgeParsing(unittest.TestCase):
    def test_reads_json_also_in_fence(self):
        self.assertTrue(parse_judgement('{"complete": true}'))
        self.assertFalse(parse_judgement('```json\n{"complete": false}\n```'))

    def test_rejects_other_shapes(self):
        for bad in ("complete", '{"complete": "yes"}', "{}"):
            with self.assertRaises(ValueError):
                parse_judgement(bad)

    def test_unreachable_endpoint_is_fail_open(self):
        judge = EndJudge(EndJudgeConfig(url="http://127.0.0.1:9/v1/chat/completions",
                                        timeout_s=0.5))
        self.assertIsNone(judge.judge("えっと明日の"))

    def test_config_follows_fix_url_and_can_be_turned_off(self):
        self.assertIsNone(EndJudgeConfig.from_env({}, fallback_url=""))
        self.assertIsNone(EndJudgeConfig.from_env({"BLUEAGENT_END_JUDGE": "0"},
                                                  fallback_url="http://x/v1"))
        config = EndJudgeConfig.from_env({}, fallback_url="http://x/v1", fallback_model="q")
        self.assertEqual((config.url, config.model), ("http://x/v1", "q"))


class _SlowJudge:
    def __init__(self, verdict, delay=0.0):
        self.verdict, self.delay = verdict, delay
        self.config = EndJudgeConfig(url="fake", timeout_s=1.0)
        self.seen = []

    def judge(self, text, previous=""):
        self.seen.append((text, previous))
        time.sleep(self.delay)
        return self.verdict


def _stt(judge, *, fix_delay=None) -> LocalSTT:
    config = SttConfig(fix_url="http://fake" if fix_delay is not None else "")
    stt = LocalSTT(config, end_judge=judge)
    stt._sense = object()                                   # 不載模型
    stt._to_float = lambda pcm: pcm                         # 不需要 numpy
    stt._run_sensevoice = lambda samples: "えっと明日の"
    if fix_delay is not None:
        def slow_fix(text, previous):
            time.sleep(fix_delay)
            return text
        stt._llm_fix = slow_fix
    return stt


PCM = b"\x00\x00" * 1600


class TestJudgeInStt(unittest.TestCase):
    def test_judge_only_when_asked(self):
        judge = _SlowJudge(False)
        stt = _stt(judge)
        self.assertIsNone(stt.transcribe(PCM).complete)
        self.assertEqual(judge.seen, [])
        result = stt.transcribe(PCM, judge=True)
        self.assertIs(result.complete, False)
        self.assertIsNotNone(result.judge_s)
        self.assertIsNotNone(result.asr_s)

    def test_judge_runs_in_parallel_with_fix(self):
        stt = _stt(_SlowJudge(True, delay=0.3), fix_delay=0.3)
        began = time.monotonic()
        result = stt.transcribe(PCM, judge=True)
        took = time.monotonic() - began
        self.assertIs(result.complete, True)
        self.assertGreaterEqual(result.fix_s, 0.29)
        self.assertLess(took, 0.55, f"判斷和糾錯要並行：花了 {took:.2f}s")


class TestSpeechHold(unittest.TestCase):
    def test_incomplete_is_merged_into_the_next(self):
        hold = SpeechHold(hold_s=2.0)
        first = hold.begin("k")
        results = {}
        worker = threading.Thread(
            target=lambda: results.setdefault("a", hold.resolve(first, "えっと明日の", False)))
        worker.start()
        time.sleep(0.15)
        second = hold.begin("k")
        results["b"] = hold.resolve(second, "天気どう？", True)
        worker.join(2)
        self.assertTrue(results["a"].superseded)
        self.assertFalse(results["b"].superseded)
        self.assertEqual(results["b"].text, "えっと明日の天気どう？")
        self.assertEqual(results["b"].merged, 1)

    def test_incomplete_without_followup_goes_after_hold(self):
        hold = SpeechHold(hold_s=0.2)
        began = time.monotonic()
        outcome = hold.resolve(hold.begin("k"), "えっと", False)
        self.assertFalse(outcome.superseded)
        self.assertEqual(outcome.text, "えっと")
        self.assertGreaterEqual(time.monotonic() - began, 0.18)

    def test_complete_or_unknown_does_not_wait(self):
        hold = SpeechHold(hold_s=5.0)
        for verdict in (True, None):
            began = time.monotonic()
            outcome = hold.resolve(hold.begin("k"), "うん", verdict)
            self.assertLess(time.monotonic() - began, 0.1)
            self.assertEqual(outcome.text, "うん")

    def test_later_sentence_waits_for_earlier_still_transcribing(self):
        hold = SpeechHold(hold_s=2.0)
        first = hold.begin("k")
        second = hold.begin("k")                         # 後一句先轉寫完
        results = {}
        worker = threading.Thread(
            target=lambda: results.setdefault("b", hold.resolve(second, "天気どう？", True)))
        worker.start()
        time.sleep(0.1)
        self.assertNotIn("b", results, "前一句還在轉寫，後一句要等它")
        results["a"] = hold.resolve(first, "明日の", False)
        worker.join(2)
        self.assertTrue(results["a"].superseded)
        self.assertEqual(results["b"].text, "明日の天気どう？")

    def test_abandon_releases_followers(self):
        hold = SpeechHold(hold_s=2.0, settle_s=3.0)
        first = hold.begin("k")
        second = hold.begin("k")
        hold.abandon(first)
        began = time.monotonic()
        self.assertEqual(hold.resolve(second, "はい", True).text, "はい")
        self.assertLess(time.monotonic() - began, 0.1)


class _CallSTT:
    """通話用的假轉寫器：每次回下一個 (文字, 說完了沒)。"""

    end_judge = object()                                    # 有裝判斷器

    def __init__(self, rows):
        self.rows = list(rows)
        self.judged = []
        self._lock = threading.Lock()

    def transcribe(self, audio, *, language="", suffix=".wav", judge=False):
        with self._lock:
            text, complete = self.rows.pop(0)
        self.judged.append(judge)
        return Transcript(text=text, seconds=1.0, engine="fake",
                          complete=complete if judge else None,
                          asr_s=0.12, fix_s=0.2, judge_s=0.15 if judge else None)

    def pcm16_to_wav(self, pcm, *, rate=16000, channels=1):
        return b"RIFF" + bytes(len(pcm))


class TestCallMerge(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        self.state.app.speech_hold = SpeechHold(hold_s=2.0)
        from agent.brain.compose import voice_api
        status, data = voice_api.call(self.state, "mira", "owner")
        self.assertEqual(status, 200, data)
        self.call_id = data["session"]["id"]

    def prepare(self):
        return api_module.prepare_audio(
            self.state, b"RIFF" + bytes(64), auth=OWNER,
            headers={"X-Call": self.call_id, "X-Entity": "mira"})

    def test_incomplete_call_sentence_merges_into_next(self):
        self.state.app.stt = _CallSTT([("えっと明日の", False), ("天気どう？", True)])
        results = {}
        worker = threading.Thread(target=lambda: results.setdefault("a", self.prepare()))
        worker.start()
        time.sleep(0.2)
        results["b"] = self.prepare()
        worker.join(3)
        status_a, data_a = results["a"]
        status_b, data_b = results["b"]
        self.assertEqual((status_a, status_b), (200, 200))
        self.assertTrue(data_a.get("merged"))
        self.assertEqual(data_b["text"], "えっと明日の天気どう？")
        self.assertEqual(data_b["transcript"]["text"], "えっと明日の天気どう？")
        timing = data_b["input_timing"]
        self.assertEqual(timing["merged"], 1)
        self.assertEqual((timing["asr_ms"], timing["fix_ms"], timing["judge_ms"]), (120, 200, 150))

    def test_voice_message_outside_call_is_not_judged(self):
        self.state.app.stt = _CallSTT([("おやすみ", True)])
        status, data = api_module.prepare_audio(self.state, b"RIFF" + bytes(64), auth=OWNER,
                                                headers={})
        self.assertEqual(status, 200)
        self.assertEqual(self.state.app.stt.judged, [False])
        self.assertIsNone(data["input_timing"]["hold_ms"])

    def test_input_timing_lands_in_turn_telemetry(self):
        self.state.app.stt = _CallSTT([("おはよう", True)])
        status, data = self.prepare()
        self.assertEqual(status, 200)
        for key in ("transcript", "input", "call"):
            data.pop(key, None)
        self.state.send(**data)
        rows = list(self.state.app.telemetry._rows)
        self.assertEqual(rows[-1]["input"]["asr_ms"], 120)
        self.assertIs(rows[-1]["input"]["complete"], True)


class TestDiscordHold(unittest.TestCase):
    def session(self, *, gap=0.1, extra=0.4):
        from agent.adapter.discord_voice import DiscordVoiceSession
        voice = DiscordVoiceSession.__new__(DiscordVoiceSession)   # 不跑 __init__（會碰 discord）
        voice.merge_gap_s = gap
        voice.hold_extra_s = extra
        voice._pending, voice._pending_at, voice._pending_incomplete = {}, {}, {}
        voice._pending_lock = threading.RLock()
        voice.merged = 0
        voice._log = lambda _message: None
        voice.sent = []
        voice._respond = lambda user_id, text: voice.sent.append((user_id, text))
        return voice

    def test_incomplete_fragment_waits_longer_and_merges(self):
        voice = self.session()
        voice._buffer_fragment("u1", "えっと明日の", complete=False)
        time.sleep(0.25)                                  # 超過 merge_gap 但還在多等的範圍裡
        self.assertEqual(voice.sent, [])
        voice._buffer_fragment("u1", "天気どう？", complete=True)
        time.sleep(0.35)
        self.assertEqual(voice.sent, [("u1", "えっと明日の天気どう？")])

    def test_complete_fragment_uses_normal_gap(self):
        voice = self.session()
        voice._buffer_fragment("u1", "おはよう", complete=True)
        time.sleep(0.3)
        self.assertEqual(voice.sent, [("u1", "おはよう")])


if __name__ == "__main__":
    unittest.main()
