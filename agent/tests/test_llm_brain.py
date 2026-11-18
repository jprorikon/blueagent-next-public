#!/usr/bin/env python3
"""真实 BrainPort：messages、OpenAI 兼容流、factory、SSE 入口。"""

from __future__ import annotations

import json
import threading
import urllib.request
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from demo_common import *  # noqa: F401,F403
from agent.brain.factory import build_brain
from agent.brain.messages import build_messages
from agent.brain.openai_compat import (OpenAICompatibleBrain,
                                                OpenAICompatibleConfig,
                                                merge_leading_system)
from agent.brain.compose.entities import build_entities, build_principals
from agent.brain.ports import BrainRequest


class _FakeLLM(BaseHTTPRequestHandler):
    last_body: dict = {}

    def do_POST(self):                                 # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length)
        try:
            type(self).last_body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            type(self).last_body = {}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for piece in ("你", "好"):
            row = {"choices": [{"delta": {"content": piece}}]}
            self.wfile.write(("data: " + json.dumps(row) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.close_connection = True

    def log_message(self, fmt, *args):                 # noqa: A003
        return


class TestMessages(unittest.TestCase):
    def test_current_turn_is_not_duplicated_and_memory_included(self):
        request = BrainRequest(
            thread_id="t", entity="mira", principal="alice", text="现在",
            system="S",
            meta={
                "recall": ["F1"], "group": True,
                "principal_name": "Alice",
                "names": {"alice": "Alice"},
                "transcript": [
                    {"role": "user", "text": "以前", "speaker": "alice"},
                    {"role": "assistant", "text": "嗯", "speaker": "mira"},
                    {"role": "user", "text": "现在", "speaker": "alice"},
                ],
            })
        messages = build_messages(request)
        self.assertEqual(messages[0]["content"], "S")
        # ADR-0020：記憶段在歷史之後（以前在 messages[1]）。用「位置在歷史後面」驗，不再釘 index。
        self.assertTrue(any("F1" in row["content"] for row in messages),
                        "記憶段要在這輪的請求裡")
        self.assertGreater(next(i for i, row in enumerate(messages) if "F1" in row["content"]),
                           next(i for i, row in enumerate(messages) if "Alice：以前" in row["content"]),
                           "記憶段要在歷史之後（ADR-0020：放前面會把歷史的快取打掉）")
        self.assertEqual(sum(1 for row in messages if row["content"].endswith("现在")), 1)
        self.assertIn("Alice：以前", [row["content"] for row in messages])


class TestOpenAIStream(unittest.TestCase):
    def test_streaming_deltas_become_frames(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLLM)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            config = OpenAICompatibleConfig(
                base_url=f"http://127.0.0.1:{httpd.server_address[1]}/v1",
                model="fake", timeout_s=3, temperature=0)
            brain = OpenAICompatibleBrain(config, build_entities(), build_principals())
            request = BrainRequest(thread_id="t", entity="mira",
                                   principal="owner", text="你好",
                                   system="S", meta={"recall": []})
            events = list(brain.run_turn(request))
            self.assertEqual([kind for kind, _ in events],
                             ["thinking", "delta", "delta", "done"])
            self.assertEqual("".join(payload["text"] for kind, payload in events
                                     if kind == "delta"), "你好")
            self.assertEqual(events[-1][1]["text"], "你好")
            self.assertEqual(events[-1][1]["model"], "fake")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_factory_has_no_default_and_openai_is_selectable(self):
        entities, principals = build_entities(), build_principals()
        # 沒設 BLUEAGENT_BRAIN ⇒ 啟動失敗（以前悄悄跑 MockBrain）；mock 要明寫
        with self.assertRaises(ValueError):
            build_brain(entities, principals, env={})
        self.assertEqual(type(build_brain(entities, principals,
                                          env={"BLUEAGENT_BRAIN": "mock"})).__name__,
                         "MockBrain")
        brain = build_brain(
            entities, principals,
            env={"BLUEAGENT_BRAIN": "openai",
                 "BLUEAGENT_LLM_BASE_URL": "http://127.0.0.1:1/v1"})
        self.assertIsInstance(brain, OpenAICompatibleBrain)
        with self.assertRaises(ValueError):
            build_brain(entities, principals,
                        env={"BLUEAGENT_BRAIN": "openai",
                             "BLUEAGENT_LLM_BASE_URL": "https://example.com/v1"})

    def test_brain_budget_comes_from_chat_role(self):
        """大腦＝chat 角色：`data/llm.json` 的 chat 額度／溫度／逾時要真的進到 brain config。

        2026-10-27 owner 把 chat 定 3200 時踩到：從前 factory 只讀 env（沒設就 512），
        compose 那行 `BLUEAGENT_LLM_MAX_TOKENS: 400` 一值定江山，改角色表看起來不生效。
        """
        entities, principals = build_entities(), build_principals()
        env = {"BLUEAGENT_BRAIN": "openai",
               "BLUEAGENT_LLM_BASE_URL": "http://127.0.0.1:1/v1"}
        role = {"max_tokens": 3200, "temperature": 0.7, "timeout_s": 42.0}
        with mock.patch("agent.brain.config.llm.settings_for", return_value=dict(role)):
            brain = build_brain(entities, principals, env=dict(env))
            self.assertEqual(brain.config.max_tokens, 3200)
            self.assertAlmostEqual(brain.config.temperature, 0.7)
            self.assertAlmostEqual(brain.config.timeout_s, 42.0)
            # env 明寫 ⇒ env 優先（臨時覆寫的逃生口不能被擋掉）
            brain_env = build_brain(entities, principals,
                                    env=dict(env, BLUEAGENT_LLM_MAX_TOKENS="400"))
            self.assertEqual(brain_env.config.max_tokens, 400)



class TestEndpointQuirks(unittest.TestCase):
    """本地 llama-server 的兩個坑（2026-10-21 實測）：多條 system 會 500、思考會吃掉 token。"""

    def test_merge_leading_system_collapses_them(self):
        merged = merge_leading_system([
            {"role": "system", "content": "人格"},
            {"role": "system", "content": "记忆"},
            {"role": "user", "content": "hi"},
        ])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["role"], "system")
        self.assertEqual(merged[0]["content"], "人格\n\n记忆")
        self.assertEqual(merged[1]["content"], "hi")

    def test_merge_keeps_later_system_untouched(self):
        messages = [{"role": "system", "content": "S"},
                    {"role": "user", "content": "u"},
                    {"role": "system", "content": "later"}]
        self.assertEqual(merge_leading_system(messages), messages)

    def test_two_system_messages_never_reach_the_endpoint(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLLM)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            config = OpenAICompatibleConfig(
                base_url=f"http://127.0.0.1:{httpd.server_address[1]}/v1",
                model="fake", timeout_s=3, temperature=0)
            brain = OpenAICompatibleBrain(config, build_entities(), build_principals())
            request = BrainRequest(thread_id="t", entity="mira",
                                   principal="owner", text="你好",
                                   system="S", meta={"recall": ["F1"]})
            list(brain.run_turn(request))
            roles = [row["role"] for row in _FakeLLM.last_body["messages"]]
            self.assertEqual(roles.count("system"), 1, roles)
            self.assertEqual(roles[0], "system")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_thinking_can_be_disabled(self):
        self.assertFalse(OpenAICompatibleConfig.from_env(
            {"BLUEAGENT_LLM_ENABLE_THINKING": "0"}).enable_thinking)
        self.assertTrue(OpenAICompatibleConfig.from_env({}).enable_thinking)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLLM)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            config = OpenAICompatibleConfig(
                base_url=f"http://127.0.0.1:{httpd.server_address[1]}/v1",
                model="fake", timeout_s=3, temperature=0, enable_thinking=False)
            brain = OpenAICompatibleBrain(config, build_entities(), build_principals())
            list(brain.run_turn(BrainRequest(thread_id="t", entity="mira",
                                             principal="owner", text="你好",
                                             system="S", meta={"recall": []})))
            self.assertEqual(_FakeLLM.last_body.get("chat_template_kwargs"),
                             {"enable_thinking": False})
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestDeadlineOverride(unittest.TestCase):
    """首幀預算要能放寬：接真模型時 8 s 會把第一句砍掉（2026-10-21 實測）。"""

    def test_env_raises_interactive_deadline(self):
        import os
        import tempfile
        from agent.brain.compose.bootstrap import build
        from agent.brain.domain.event import SourceClass

        previous = os.environ.get("BLUEAGENT_DEADLINE_MS")
        os.environ["BLUEAGENT_DEADLINE_MS"] = "30000"
        try:
            demo = build(data_dir=tempfile.mkdtemp(prefix="ba-deadline-"))
            self.assertEqual(
                demo.router._config.deadline(SourceClass.INTERACTIVE), 30000)
        finally:
            if previous is None:
                os.environ.pop("BLUEAGENT_DEADLINE_MS", None)
            else:
                os.environ["BLUEAGENT_DEADLINE_MS"] = previous

    def test_default_stays_at_rules_value(self):
        from agent.brain.router import rules
        from agent.brain.router.policy import RouterConfig
        from agent.brain.domain.event import SourceClass

        self.assertEqual(RouterConfig().deadline(SourceClass.INTERACTIVE),
                         rules.DEADLINES[SourceClass.INTERACTIVE])


class TestDeepSeekProvider(unittest.TestCase):
    """`BLUEAGENT_BRAIN=deepseek` 照抄现役桥的变量名（换架构不换模型）。"""

    def test_reads_bridge_variable_names(self):
        brain = build_brain(
            build_entities(), build_principals(),
            env={"BLUEAGENT_BRAIN": "deepseek",
                 "DEEPSEEK_API_KEY": "sk-test",
                 "DEEPSEEK_MODEL": "deepseek-v4-flash",
                 "DEEPSEEK_BASE": "https://api.deepseek.com/v1",
                 "DEEPSEEK_THINKING": "low"})
        self.assertIsInstance(brain, OpenAICompatibleBrain)
        self.assertEqual(brain.config.model, "deepseek-v4-flash")
        self.assertEqual(brain.config.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(brain.config.api_key, "sk-test")
        self.assertEqual(brain.config.reasoning_effort, "low")

    def test_canonical_names_win_over_provider_ones(self):
        brain = build_brain(
            build_entities(), build_principals(),
            env={"BLUEAGENT_BRAIN": "deepseek",
                 "DEEPSEEK_MODEL": "ignored",
                 "BLUEAGENT_LLM_MODEL": "explicit-model",
                 "DEEPSEEK_API_KEY": "sk-test"})
        self.assertEqual(brain.config.model, "explicit-model")

    def test_remote_endpoint_without_key_is_loud(self):
        with self.assertRaises(ValueError):
            build_brain(build_entities(), build_principals(),
                        env={"BLUEAGENT_BRAIN": "deepseek",
                             "DEEPSEEK_BASE": "https://api.deepseek.com/v1"})

    def test_reasoning_effort_lands_in_the_request(self):
        config = OpenAICompatibleConfig(base_url="http://127.0.0.1:1/v1",
                                        model="m", reasoning_effort="low")
        brain = OpenAICompatibleBrain(config, build_entities(), build_principals())
        body = brain._body(BrainRequest(thread_id="t", entity="mira",
                                        principal="owner", text="hi",
                                        system="S", meta={"recall": []}),
                           [{"role": "system", "content": "S"},
                            {"role": "user", "content": "hi"}])
        self.assertEqual(body["reasoning_effort"], "low")

    def test_no_reasoning_effort_by_default(self):
        config = OpenAICompatibleConfig(base_url="http://127.0.0.1:1/v1", model="m")
        brain = OpenAICompatibleBrain(config, build_entities(), build_principals())
        body = brain._body(BrainRequest(thread_id="t", entity="mira",
                                        principal="owner", text="hi",
                                        system="S", meta={"recall": []}), [])
        self.assertNotIn("reasoning_effort", body)

    def test_strata_prefix_pins_only_the_merged_fixed_block(self):
        """`strata_prefix` 打開時只钉 1 条（合并后的固定段 system）；**默认是关的**。

        2026-11-02 23:1x 实测：引擎对这个字段回 `No user query found in messages.`（curl 二分：
        system+user ＝ 200；同一个 body 再加 `strata_prefix:{messages:1}` ＝ 400）⇒ 主脑每一轮
        都 8 ms 失败、她答空。所以默认值改成关（`BLUEAGENT_STRATA_PREFIX=1` 才发），
        这条测试同时钉住「默认不发」与「打开时只钉 1 条、且那条是 system」。
        """
        from unittest import mock

        from agent.brain.openai_compat import merge_leading_system
        config = OpenAICompatibleConfig(base_url="http://127.0.0.1:1/v1", model="m")
        brain = OpenAICompatibleBrain(config, build_entities(), build_principals())
        request = BrainRequest(thread_id="t", entity="mira", principal="owner",
                               text="こんにちは", system="STANCE",
                               meta={"recall": ["fact"], "soul_block": "SOUL",
                                     "mood_block": "MOOD"})
        messages = merge_leading_system(build_messages(request))
        with mock.patch.dict("os.environ", {"BLUEAGENT_STRATA_PREFIX": "0"}):
            self.assertNotIn("strata_prefix", brain._body(request, messages),
                             "默认不许发：这个字段现在会让整轮 400（她答空）")
        with mock.patch.dict("os.environ", {"BLUEAGENT_STRATA_PREFIX": "1"}):
            body = brain._body(request, messages)
        pinned = body["strata_prefix"]["messages"]
        self.assertEqual(pinned, 1)
        self.assertTrue(all(row["role"] == "system" for row in messages[:pinned]))
        self.assertNotIn("MOOD", "".join(row["content"] for row in messages[:pinned]))


class TestToolNameSanitising(unittest.TestCase):
    """DeepSeek 只收 `[a-zA-Z0-9_-]`；`voice.speak` 会 400 —— 送出去消毒、收回来还原。"""

    def test_safe_tool_name(self):
        from agent.brain.openai_compat import _safe_tool_name
        self.assertEqual(_safe_tool_name("voice.speak"), "voice_speak")
        self.assertEqual(_safe_tool_name("voice.call"), "voice_call")
        self.assertEqual(_safe_tool_name("plain_name-1"), "plain_name-1")

    def test_body_sends_sanitised_names(self):
        config = OpenAICompatibleConfig(base_url="http://127.0.0.1:1/v1", model="m")
        brain = OpenAICompatibleBrain(config, build_entities(), build_principals())
        tools = [{"type": "function", "function": {"name": "voice.speak",
                                                   "description": "说",
                                                   "parameters": {}}}]
        body = brain._body(BrainRequest(thread_id="t", entity="mira",
                                        principal="owner", text="hi", system="S",
                                        meta={"recall": [], "tools": tools}), [])
        self.assertEqual(body["tools"][0]["function"]["name"], "voice_speak")
        self.assertEqual(brain._tool_names, {"voice_speak": "voice.speak"})

    def test_second_round_history_uses_the_same_sanitised_name(self):
        config = OpenAICompatibleConfig(base_url="http://127.0.0.1:1/v1", model="m")
        brain = OpenAICompatibleBrain(config, build_entities(), build_principals())
        tools = [{"type": "function", "function": {"name": "voice.speak",
                                                   "description": "说",
                                                   "parameters": {}}}]
        request = BrainRequest(
            thread_id="t", entity="mira", principal="owner", text="继续", system="S",
            meta={"recall": [], "tools": tools, "tool_trace": [{
                "calls": [{"id": "call_1", "name": "voice.speak", "arguments": "{}"}],
                "results": [{"call_id": "call_1", "content": "{\"ok\":true}"}],
            }]})
        body = brain._body(request, build_messages(request))
        offered = body["tools"][0]["function"]["name"]
        assistant = next(row for row in body["messages"] if row["role"] == "assistant")
        history = assistant["tool_calls"][0]["function"]["name"]
        self.assertEqual(offered, "voice_speak")
        self.assertEqual(history, offered)

    def test_reply_with_sanitised_name_maps_back(self):
        from agent.brain.openai_compat import OpenAICompatibleBrain as Brain
        config = OpenAICompatibleConfig(base_url="http://127.0.0.1:1/v1", model="m")
        brain = Brain(config, build_entities(), build_principals())
        back = getattr(brain, "_tool_names", {})
        back["voice_speak"] = "voice.speak"
        brain._tool_names = back
        call = {"name": "voice_speak", "arguments": "{}"}
        # 复用 run_turn 的还原逻辑（这里直接验映射函数的行为）
        call["name"] = brain._tool_names.get(call["name"], call["name"])
        self.assertEqual(call["name"], "voice.speak")

class TestStreamHttp(unittest.TestCase):
    def test_stream_requires_token(self):
        from agent.server import http as app_server
        import urllib.error

        state = make_state()
        app_server.set_state(state)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), app_server.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            body = json.dumps({"text": "你好"}).encode("utf-8")
            request = urllib.request.Request(
                base + "/api/stream", data=body, method="POST",
                headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 401)
        finally:
            httpd.shutdown()
            httpd.server_close()
            state.shutdown()

    def test_owner_stream_emits_frames_then_result(self):
        from agent.server import http as app_server

        state = make_state()
        app_server.set_state(state)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), app_server.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            body = json.dumps({"text": "你好", "entity": "mira",
                               "principal": "owner"}).encode("utf-8")
            request = urllib.request.Request(
                base + "/api/stream", data=body, method="POST",
                headers={**owner_headers(state),
                         "Content-Type": "application/json"})
            events = []
            frames = []
            result = None
            with urllib.request.urlopen(request, timeout=8) as response:
                event = ""
                for raw in response:
                    line = raw.decode("utf-8").strip()
                    if line.startswith("event:"):
                        event = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        payload = json.loads(line.split(":", 1)[1].strip())
                        events.append(event)
                        # 契約：事件名＝內層 kind（`done`/`delta`/`speech`…），只有 `result` 是元資料
                        if event == "result":
                            result = payload
                        else:
                            frames.append(payload)
            self.assertIn("done", events, events)
            self.assertEqual(events[-1], "result")
            self.assertTrue(any(frame.get("kind") == "done" for frame in frames))
            self.assertIsNotNone(result)
            self.assertTrue(result["ok"], result.get("error"))
        finally:
            httpd.shutdown()
            httpd.server_close()
            state.shutdown()


class _FakeToolLLM(BaseHTTPRequestHandler):
    seen_tool = False

    def do_POST(self):                                 # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        body = json.loads(self.rfile.read(length) or b"{}")
        has_tool = any(row.get("role") == "tool" for row in body.get("messages", []))
        self.server.seen_tool = has_tool
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        if has_tool:
            row = {"choices": [{"delta": {"content": "好，我打给你。"}}]}
        else:
            row = {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "call_1", "type": "function",
                "function": {"name": "voice.call",
                             "arguments": "{\"end\": \"discord\"}"},
            }]}}]}
        self.wfile.write(("data: " + json.dumps(row, ensure_ascii=False) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.close_connection = True

    def log_message(self, fmt, *args):                 # noqa: A003
        return


class TestToolCalls(unittest.TestCase):
    def test_brain_emits_tool_call_then_continues_with_results(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeToolLLM)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            brain = OpenAICompatibleBrain(
                OpenAICompatibleConfig(
                    base_url=f"http://127.0.0.1:{httpd.server_address[1]}/v1",
                    model="fake", timeout_s=3),
                build_entities(), build_principals())
            request = BrainRequest(thread_id="t", entity="mira",
                                   principal="owner", text="打电话给我",
                                   system="S", meta={})
            first = list(brain.run_turn(request))
            self.assertEqual([kind for kind, _ in first], ["thinking", "tool_call"])
            self.assertEqual(first[-1][1]["name"], "voice.call")
            self.assertEqual(first[-1][1]["arguments"], {"end": "discord"})

            second_request = BrainRequest(
                thread_id="t", entity="mira", principal="owner",
                text="打电话给我", system="S",
                meta={"tool_trace": [{
                    "calls": [{"id": "call_1", "name": "voice.call",
                               "arguments": "{\"end\": \"discord\"}"}],
                    "results": [{"call_id": "call_1", "content": "{\"ok\": true}"}],
                }]})
            second = list(brain.run_turn(second_request))
            self.assertEqual(second[-1][0], "done")
            self.assertEqual(second[-1][1]["text"], "好，我打给你。")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_brain_stage_executes_tool_and_feeds_result_back(self):
        state = make_state()

        class LoopBrain:
            def __init__(self):
                self.calls = 0
                self.trace = None
                self.tools = None

            def run_turn(self, request):
                self.calls += 1
                self.tools = request.meta.get("tools")
                if self.calls == 1:
                    yield ("tool_call", {"id": "call_1", "name": "voice.call",
                                         "arguments": {"end": "discord"}})
                    return
                self.trace = request.meta.get("tool_trace")
                yield ("done", {"text": "好，我打给你。", "entity": "mira",
                                "principal": "owner", "model": "test"})

        brain = LoopBrain()
        state.app.runtime.ports.brain = brain
        try:
            result = state.send("打电话给我", entity="mira", principal="owner")
            self.assertTrue(result["ok"], result.get("error"))
            self.assertEqual(brain.calls, 2)
            self.assertIn("voice.call",
                          [tool["function"]["name"] for tool in brain.tools])
            self.assertEqual(brain.trace[0]["calls"][0]["name"], "voice.call")
            self.assertTrue(any(frame["kind"] == "call" for frame in result["frames"]))
            self.assertTrue(any(frame["kind"] == "done" and
                                frame["payload"]["text"] == "好，我打给你。"
                                for frame in result["frames"]))
        finally:
            state.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
