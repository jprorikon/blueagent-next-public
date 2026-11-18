#!/usr/bin/env python3
"""首幀預算：**8 秒只管「第一個字」，不再當每次請求的超時**（owner 2026-11-02 重排）。

背景（實測 22:08）：主人在 App 說一句，`subagents 5904ms → brain 38ms`、`first_token=null`、
出口空字串。追下去發現 `deadline_ms`（名義「首幀預算」）被當成**每一次** LLM 請求的
HTTP 超時在用——工具子回合的第 2、3 次請求也只有 8 秒，而那时 prompt 更長。

判據（這一支守的）：
  ① 第一個字沒在首幀預算內到 ⇒ 明確發 `error` 幀（`first_token_timeout`），不再乾等、
     也不再被誤判成 `network_error`；
  ② 同樣的慢串流，**不設 deadline** 時要能完整跑完（證明沒有別的東西在拿它當超時）；
  ③ 已經出過字之後才斷 ⇒ 仍然算 `network_error`（不能把兩種混成一個碼）。
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.entities import build_entities, build_principals
from agent.brain.openai_compat import (OpenAICompatibleBrain,
                                                OpenAICompatibleConfig)
from agent.brain.ports import BrainRequest


class _SlowFirstToken(BaseHTTPRequestHandler):
    """收到請求後先沉默 `delay` 秒，才吐第一個字。"""

    delay = 1.2
    pieces = ("你", "好")

    def do_POST(self):                                 # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        time.sleep(type(self).delay)
        for piece in type(self).pieces:
            row = {"choices": [{"delta": {"content": piece}}]}
            self.wfile.write(("data: " + json.dumps(row) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.close_connection = True

    def log_message(self, fmt, *args):                 # noqa: A003
        return


class _ThinksThenAnswers(BaseHTTPRequestHandler):
    """先吐一段比首幀預算還長的思考流（reasoning_content），才吐正文。"""

    beats, gap = 6, 0.1
    pieces = ("你", "好")

    def do_POST(self):                                 # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        rows = [{"choices": [{"delta": {"reasoning_content": "うーん"}}]}
                for _ in range(type(self).beats)]
        rows += [{"choices": [{"delta": {"content": piece}}]} for piece in type(self).pieces]
        for row in rows:
            self.wfile.write(("data: " + json.dumps(row) + "\n\n").encode())
            self.wfile.flush()
            time.sleep(type(self).gap)
        self.wfile.write(b"data: [DONE]\n\n")
        self.close_connection = True

    def log_message(self, fmt, *args):                 # noqa: A003
        return


class TestFirstTokenBudget(unittest.TestCase):
    def _brain(self, server, *, timeout_s: float = 5.0):
        config = OpenAICompatibleConfig(
            base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            model="fake", timeout_s=timeout_s, temperature=0)
        return OpenAICompatibleBrain(config, build_entities(), build_principals())

    def _serve(self, handler):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    def test_first_token_timeout_is_reported_as_its_own_error(self):
        httpd = self._serve(_SlowFirstToken)
        try:
            brain = self._brain(httpd)
            request = BrainRequest(thread_id="t", entity="mira", principal="owner",
                                   text="你好", system="S", meta={"recall": []},
                                   deadline_ms=300)
            events = list(brain.run_turn(request))
        finally:
            httpd.shutdown()
            httpd.server_close()
        codes = [payload.get("error") for kind, payload in events if kind == "error"]
        self.assertEqual(codes, ["first_token_timeout"],
                         "首幀沒到要說得出來是哪一種，不能混成 network_error")
        done = [payload for kind, payload in events if kind == "done"]
        self.assertEqual(done[-1].get("text"), "", "超時就不該有正文")
        self.assertFalse(any(kind == "delta" for kind, _ in events))

    def test_slow_stream_completes_when_no_deadline(self):
        httpd = self._serve(_SlowFirstToken)
        try:
            brain = self._brain(httpd, timeout_s=10.0)
            request = BrainRequest(thread_id="t", entity="mira", principal="owner",
                                   text="你好", system="S", meta={"recall": []})
            events = list(brain.run_turn(request))
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual("".join(payload["text"] for kind, payload in events
                                 if kind == "delta"), "你好",
                         "不設首幀預算時，慢一點的串流要照樣跑完（不能被誰當超時砍掉）")
        self.assertEqual([payload.get("error") for kind, payload in events if kind == "error"], [])

    def test_thinking_past_the_budget_is_not_a_timeout(self):
        """思考流在跑 ⇒ 她在想、不是在掛：首幀預算過了也不能判 `first_token_timeout`。

        2026-11-03：主腦開了 `reasoning_effort=low`，首個正文常在思考之後才到；
        思考不算輸出的話，這種輪次會整輪答空。
        """
        httpd = self._serve(_ThinksThenAnswers)
        try:
            brain = self._brain(httpd)
            request = BrainRequest(thread_id="t", entity="mira", principal="owner",
                                   text="你好", system="S", meta={"recall": []},
                                   deadline_ms=300)
            events = list(brain.run_turn(request))
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual([payload.get("error") for kind, payload in events if kind == "error"], [])
        self.assertEqual("".join(payload["text"] for kind, payload in events
                                 if kind == "delta"), "你好")


if __name__ == "__main__":
    unittest.main()
