#!/usr/bin/env python3
"""說話語言約束：`BLUEAGENT_SPEAK_LANG`（文字與聲音用同一種語言）。

判據：
  ① 設 `ja` ⇒ `env.meta["speak_lang"]="ja"`，且**人格段不被塞東西**（實測塞了反而退化）；
  ② 真正注入在最後一句用戶訊息尾端（`messages.TURN_SUFFIX`），**transcript 原文不動**；
  ③ 不設 ⇒ messages 一字不改（132 項原測試不受影響）；
  ④ 未知語言碼 ⇒ 不注入；
  ⑤ 中文台詞 + 日文聲線時，TTS adapter 提醒一次（不讓亂碼悄悄上線）。
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import tempfile
import unittest
from types import SimpleNamespace

from demo_common import *  # noqa: F401,F403

import json
from http.server import BaseHTTPRequestHandler

from agent.brain.messages import TURN_SUFFIX, build_messages
from agent.brain.compose import stance_seed
from agent.brain.compose.entities import (build_entities, build_principals,
                                          build_social)
from agent.brain.compose.stages import PersonaStage
from agent.brain.domain.event import IngressEvent, InputKind, Origin, SourceClass
from agent.brain.ports import BrainRequest
from agent.brain.runtime.stance import StanceService
from agent.brain.runtime.stance_store import StanceStore


def make_stage(speak_lang: str = "") -> PersonaStage:
    stances = StanceService(StanceStore(pathlib.Path(tempfile.mkdtemp(prefix="ba-stance-"))),
                            stance_seed.build_catalog())
    stance_seed.seed(stances)
    return PersonaStage(build_entities(), build_principals(), build_social(),
                        stances, speak_lang=speak_lang)


def make_env(text: str = "你好呀"):
    """只給 PersonaStage 真的會用的欄位（它不看 ctx）。"""
    event = IngressEvent(principal="owner", entity="mira", source="phone:owner",
                         kind=InputKind.TEXT, at=0.0, source_class=SourceClass.INTERACTIVE,
                         origin=Origin.OWNED, participants=("owner",))
    decision = SimpleNamespace(entity="mira", principal="owner", scope="",
                               addressed_to="")
    thread = SimpleNamespace(transcript=[], id="th_1")
    return SimpleNamespace(event=event, decision=decision, thread=thread, meta={},
                           text=text)


class TestPersonaSide(unittest.TestCase):
    def test_japanese_lang_only_sets_meta(self):
        env = make_env()
        make_stage("ja").run(env, None)
        self.assertEqual(env.meta["speak_lang"], "ja")
        # 實測：規則塞人格段（頭或尾）都會被打回中文，還會退化 ⇒ 人格段保持乾淨
        self.assertNotIn("日本語", env.meta["persona"])

    def test_no_lang_keeps_everything_clean(self):
        env = make_env()
        make_stage("").run(env, None)
        self.assertNotIn("speak_lang", env.meta)

    def test_unknown_language_is_ignored(self):
        env = make_env()
        make_stage("klingon").run(env, None)
        self.assertNotIn("speak_lang", env.meta)


class TestTurnSuffix(unittest.TestCase):
    def request(self, speak_lang: str = "", text: str = "你好呀") -> BrainRequest:
        return BrainRequest(thread_id="t", entity="mira", principal="owner",
                            text=text, system="S",
                            meta={"recall": [], "speak_lang": speak_lang})

    def test_japanese_suffix_lands_on_last_user_turn(self):
        messages = build_messages(self.request("ja"))
        self.assertTrue(messages[-1]["content"].endswith(TURN_SUFFIX["ja"]))
        self.assertIn("你好呀", messages[-1]["content"])

    def test_no_suffix_when_unset(self):
        # 最後一條前面會併著本輪段（2026-11-02），所以只看尾巴：以這句話結束、沒有語言鎖
        content = build_messages(self.request(""))[-1]["content"]
        self.assertTrue(content.endswith("你好呀"))
        self.assertFalse(any(suffix in content for suffix in TURN_SUFFIX.values() if suffix))

    def test_chinese_suffix_exists(self):
        self.assertTrue(build_messages(self.request("zh"))[-1]["content"]
                        .endswith(TURN_SUFFIX["zh"]))

    def test_suffix_is_not_duplicated(self):
        text = "你好呀" + TURN_SUFFIX["ja"]
        self.assertEqual(build_messages(self.request("ja", text))[-1]["content"].count("日本語で答えて"), 1)

    def test_transcript_text_is_stored_verbatim(self):
        """記憶裡那一句必須是原文（不能把指令污染進 transcript）。"""
        request = self.request("ja")
        request.meta["transcript"] = [{"role": "user", "text": "上一句", "speaker": "owner"}]
        messages = build_messages(request)
        # ADR-0020：歷史在「本輪段」之前，本輪段在最後那條用戶訊息之前
        self.assertIn("上一句", [row["content"] for row in messages],
                      "transcript 要原樣進 prompt")



class _ScriptedLLM(BaseHTTPRequestHandler):
    """第一次回中文、之后回日语——用来验语言闸会不会把中文拦在缓冲里。"""

    replies: list[str] = []
    bodies: list[dict] = []

    def do_POST(self):                                     # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        type(self).bodies.append(json.loads(self.rfile.read(length).decode() or "{}"))
        reply = type(self).replies.pop(0) if type(self).replies else "おやすみ。"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for piece in [reply[i:i + 3] for i in range(0, len(reply), 3)]:
                row = {"choices": [{"delta": {"content": piece}}]}
                self.wfile.write(("data: " + json.dumps(row) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
        except BrokenPipeError:
            pass          # 語言閘判定不符時客戶端會主動斷流，這是預期行為
        self.close_connection = True

    def log_message(self, *_args):
        return None


class TestLanguageGuard(unittest.TestCase):
    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer
        _ScriptedLLM.replies = []
        _ScriptedLLM.bodies = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedLLM)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def brain(self):
        from agent.brain.openai_compat import (OpenAICompatibleBrain,
                                                        OpenAICompatibleConfig)
        config = OpenAICompatibleConfig(
            base_url=f"http://127.0.0.1:{self.httpd.server_address[1]}/v1",
            model="fake", timeout_s=5, temperature=0, max_tokens=80)
        return OpenAICompatibleBrain(config, build_entities(), build_principals())

    def request(self, speak_lang="ja", text="晚安"):
        return BrainRequest(thread_id="t", entity="mira", principal="owner",
                            text=text, system="S",
                            meta={"recall": [], "speak_lang": speak_lang})

    def test_verdict_basics(self):
        from agent.brain.openai_compat import _lang_verdict
        self.assertTrue(_lang_verdict("おはよう", "ja"))
        self.assertFalse(_lang_verdict("老师今天有空呀，快来陪我", "ja"))
        self.assertIsNone(_lang_verdict("老", "ja"))
        self.assertFalse(_lang_verdict("おはよう", "zh"))
        self.assertTrue(_lang_verdict("你好", "zh"))
        self.assertTrue(_lang_verdict("anything", ""))     # 没设约束就一律放行

    def test_rewrite_hint_lands_on_last_user_turn(self):
        from agent.brain.openai_compat import _rewrite_hint
        messages = [{"role": "system", "content": "S"},
                    {"role": "user", "content": "u1"},
                    {"role": "assistant", "content": "a"},
                    {"role": "user", "content": "u2"}]
        out = _rewrite_hint(messages, "ja")
        self.assertTrue(out[-1]["content"].startswith("u2"))
        self.assertIn("日本語だけで書き直して", out[-1]["content"])
        self.assertEqual(out[1]["content"], "u1")          # 只动最后一句
        self.assertEqual(out[-1]["content"], messages[-1]["content"] + out[-1]["content"][len("u2"):])

    def test_chinese_reply_is_held_back_and_retried(self):
        _ScriptedLLM.replies = ["老师今天有空呀，快来陪我嘛", "おやすみなさい、オーナー。"]
        events = list(self.brain().run_turn(self.request()))
        kinds = [kind for kind, _ in events]
        self.assertIn("lang_retry", kinds)
        deltas = "".join(payload["text"] for kind, payload in events if kind == "delta")
        self.assertNotIn("老师", deltas, f"中文不该流到端末：{deltas!r}")
        self.assertIn("おやすみ", deltas)
        self.assertEqual(kinds[-1], "done")
        self.assertIn("おやすみ", events[-1][1]["text"])
        # 第二次请求带了「重写成日语」的提示
        self.assertIn("日本語だけで書き直して", _ScriptedLLM.bodies[-1]["messages"][-1]["content"])

    def test_empty_response_is_retried(self):
        """模型一个字都没给（思考吃光 token／偶发空回）⇒ 再要一次，不给用户看空回复。"""
        _ScriptedLLM.replies = ["", "おやすみ。"]
        events = list(self.brain().run_turn(self.request(speak_lang="")))
        self.assertIn("retry", [kind for kind, _ in events])
        self.assertEqual(len(_ScriptedLLM.bodies), 2)
        self.assertIn("おやすみ", events[-1][1]["text"])

    def test_accepts_japanese_first_time(self):
        _ScriptedLLM.replies = ["おやすみ、オーナー。"]
        events = list(self.brain().run_turn(self.request()))
        self.assertNotIn("lang_retry", [kind for kind, _ in events])
        self.assertEqual(len(_ScriptedLLM.bodies), 1)

    def test_two_failures_fall_through_to_text(self):
        """兩次都不符就照原樣給字（寧可語言不對，也不能整輪沒字）。"""
        _ScriptedLLM.replies = ["老师今天有空呀快来陪我", "老师今天也没空呀"]
        events = list(self.brain().run_turn(self.request()))
        deltas = "".join(payload["text"] for kind, payload in events if kind == "delta")
        self.assertEqual(len(_ScriptedLLM.bodies), 2)
        self.assertIn("老师", deltas)
        self.assertEqual([kind for kind, _ in events][-1], "done")

class TestMismatchWarning(unittest.TestCase):
    def make_tts(self):
        from agent.adapter.tts_bridge import BridgeConfig, BridgeTts
        return BridgeTts(BridgeConfig(bridge_root=pathlib.Path("/nonexistent"),
                                      assets=pathlib.Path("/nonexistent"),
                                      url="http://127.0.0.1:9", text_lang="ja"))

    def test_chinese_sentence_with_japanese_voice_warns_once(self):
        tts = self.make_tts()
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            tts._warn_if_mismatch("老师，晚安。", "ja")
            tts._warn_if_mismatch("老师，晚安。", "ja")
        self.assertEqual(buffer.getvalue().count("念歪"), 1, buffer.getvalue())

    def test_japanese_sentence_does_not_warn(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            self.make_tts()._warn_if_mismatch("おはよう。元気？", "ja")
        self.assertEqual(buffer.getvalue(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
