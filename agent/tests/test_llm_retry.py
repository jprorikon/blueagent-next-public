#!/usr/bin/env python3
"""主腦請求的重試：還沒有輸出時遇到 408／429／5xx 才重送；有輸出之後、或要等太久就直接失敗。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from demo_common import *  # noqa: F401,F403
from agent.brain.compose.entities import build_entities, build_principals
from agent.brain.openai_compat import OpenAICompatibleBrain, OpenAICompatibleConfig
from agent.brain.ports import BrainRequest


class _Flaky(BaseHTTPRequestHandler):
    """前 `fail_times` 次回 `fail_status`（可帶 Retry-After），之後正常串流。"""
    fail_times = 0
    fail_status = 429
    retry_after = ""
    calls = 0

    def do_POST(self):                                 # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or "0"))
        cls = type(self)
        cls.calls += 1
        if cls.calls <= cls.fail_times:
            body = b'{"error":"busy"}'
            self.send_response(cls.fail_status)
            if cls.retry_after:
                self.send_header("Retry-After", cls.retry_after)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        row = {"choices": [{"delta": {"content": "你好呀"}}]}
        self.wfile.write(("data: " + json.dumps(row) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.close_connection = True

    def log_message(self, fmt, *args):                 # noqa: A003
        return


class TestBrainRetry(unittest.TestCase):
    def setUp(self):
        _Flaky.calls = 0
        _Flaky.retry_after = ""
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Flaky)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        config = OpenAICompatibleConfig(
            base_url=f"http://127.0.0.1:{self.httpd.server_address[1]}/v1",
            model="fake", timeout_s=3, temperature=0)
        self.brain = OpenAICompatibleBrain(config, build_entities(), build_principals())
        self.sleeps: list[float] = []
        self.brain._sleep = self.sleeps.append

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _run(self, deadline_ms: int = 0):
        request = BrainRequest(thread_id="t", entity="mira", principal="owner",
                               text="你好", deadline_ms=deadline_ms)
        return list(self.brain.run_turn(request))

    def test_429_then_ok_retries_with_default_backoff(self):
        _Flaky.fail_times, _Flaky.fail_status = 2, 429
        events = self._run()
        self.assertEqual(self.sleeps, [3.0, 6.0])
        retries = [payload for kind, payload in events if kind == "retry"]
        self.assertEqual([row["reason"] for row in retries], ["http_429", "http_429"])
        self.assertEqual(events[-1][0], "done")
        self.assertEqual(events[-1][1]["text"], "你好呀")

    def test_retry_after_header_is_used(self):
        _Flaky.fail_times, _Flaky.fail_status, _Flaky.retry_after = 1, 503, "1.5"
        events = self._run()
        self.assertEqual(self.sleeps, [1.5])
        self.assertEqual(events[-1][1]["text"], "你好呀")

    def test_retry_after_too_long_fails_at_once(self):
        _Flaky.fail_times, _Flaky.fail_status, _Flaky.retry_after = 1, 429, "45"
        events = self._run()
        self.assertEqual(self.sleeps, [])
        self.assertEqual(_Flaky.calls, 1)
        self.assertEqual(events[-1][1].get("error"), "http_429")

    def test_gives_up_after_three_retries(self):
        _Flaky.fail_times, _Flaky.fail_status = 10, 502
        events = self._run()
        self.assertEqual(self.sleeps, [3.0, 6.0, 12.0])
        self.assertEqual(_Flaky.calls, 4)
        self.assertEqual(events[-1][1].get("error"), "http_502")
        # 最後失敗的那一下仍然帶得到伺服器回的內容（body 在重試時已讀過）
        errors = [payload for kind, payload in events if kind == "error"]
        self.assertIn("busy", errors[-1]["text"])

    def test_client_error_is_not_retried(self):
        _Flaky.fail_times, _Flaky.fail_status = 1, 400
        events = self._run()
        self.assertEqual(self.sleeps, [])
        self.assertEqual(events[-1][1].get("error"), "http_400")

    def test_wait_past_deadline_fails_at_once(self):
        _Flaky.fail_times, _Flaky.fail_status = 1, 429
        events = self._run(deadline_ms=2000)            # 預設要等 3 秒 > 2 秒 deadline
        self.assertEqual(self.sleeps, [])
        self.assertEqual(events[-1][1].get("error"), "http_429")


if __name__ == "__main__":
    unittest.main()
