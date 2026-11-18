#!/usr/bin/env python3
"""Demo：HTTP API"""

from __future__ import annotations

from demo_common import *  # noqa: F401,F403


class TestHttp(unittest.TestCase):
    def test_static_state_send_seal(self):
        from agent.server import http as app_server

        state = make_state()
        app_server.set_state(state)
        owner = owner_headers(state)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), app_server.Handler)
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/", timeout=3) as response:
                html = response.read().decode("utf-8")
            self.assertIn("BlueAgent Next", html)

            with urllib.request.urlopen(
                    urllib.request.Request(base + "/api/state", headers=owner),
                    timeout=3) as response:
                payload = json.loads(response.read())
            self.assertIn("entities", payload)
            self.assertEqual(payload["engine"] if "engine" in payload else "blueagent-next",
                             "blueagent-next")

            body = json.dumps({"text": "你好", "entity": "mira",
                               "principal": "owner"}).encode("utf-8")
            request = urllib.request.Request(
                base + "/api/send", data=body, method="POST",
                headers={**owner, "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.loads(response.read())
            self.assertTrue(result["ok"], result.get("error"))
            self.assertTrue(done_text(result))

            seal_request = urllib.request.Request(
                base + "/api/seal",
                data=json.dumps({"entity": "mira", "principal": "owner"}).encode("utf-8"),
                method="POST", headers={**owner, "Content-Type": "application/json"})
            with urllib.request.urlopen(seal_request, timeout=3) as response:
                sealed = json.loads(response.read())
            self.assertTrue(sealed["ok"])

            activity_request = urllib.request.Request(
                base + "/api/activity/start",
                data=json.dumps({"entity": "mira", "principal": "owner"}).encode("utf-8"),
                method="POST", headers={**owner, "Content-Type": "application/json"})
            with urllib.request.urlopen(activity_request, timeout=3) as response:
                started = json.loads(response.read())
            self.assertTrue(started["ok"])
            self.assertIn("activity", started["life"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            time.sleep(0.05)


if __name__ == "__main__":
    unittest.main(verbosity=2)
