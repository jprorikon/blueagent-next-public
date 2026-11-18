#!/usr/bin/env python3
"""委任本體（M4）：主人開 work 模式 ⇒ 她真的能叫 Hermes 幹活。

判據：
  ① **只有主人**：guest 拿不到工具（`schemas` 裡沒有）、直接叫也回 `work_requires_owner`；
  ② **工具面按模式收窄**：chat 模式下 `delegate` 不出現；
  ③ **`{{OUT}}` 占位符**：模型只寫占位符，代码替換成約定名；扩展名由模型自己加；
  ④ **回程剥路径**：交给模型的那份没有內部路径；给前端的 `artifact` 帧仍带路径；
  ⑤ **交付回执**：产物真的出现了 ⇒ 「已交付」；没出现 ⇒ 「未交付」（回执不含路径）；
  ⑥ **去重**：同一条 thread 同一句话不重复委任；
  ⑦ **代理審批**：无人值守的 run 不会卡在 `waiting_for_approval`；
  ⑧ **超时**：不挂死，回报 timeout。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from demo_common import *  # noqa: F401,F403

from agent.adapter.hermes_delegate import HermesGateway
from agent.brain.runtime.delegate import (DelegateService, RECEIPT_DELIVERED,
                                             RECEIPT_MISSING, strip_paths)


class FakeHermes(BaseHTTPRequestHandler):
    script: dict = {}
    runs: list[dict] = []
    approvals: list[dict] = []
    counter = 0

    def do_POST(self):                                    # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        body = json.loads(self.rfile.read(length).decode() or "{}")
        if self.path == "/v1/runs":
            type(self).counter += 1
            run_id = f"run-{type(self).counter}"
            type(self).runs.append({"id": run_id, "body": body})
            self._json({"run_id": run_id})
            return
        if self.path.endswith("/approval"):
            type(self).approvals.append(body)
            self._json({"resolved": True})
            return
        self.send_error(404)

    def do_GET(self):                                     # noqa: N802
        if self.path == "/health":
            self._json({"status": "ok"})
            return
        if self.path.endswith("/events"):
            run_id = self.path.split("/runs/")[1].split("/")[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for frame in type(self).script.get(run_id, []):
                self.wfile.write(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.01)
                if frame.get("event") == "__hang__":
                    time.sleep(float(frame.get("seconds") or 6))
                    return
                if frame.get("event") == "approval.required":
                    # 真的卡住：沒被代答就不往下走（這才叫「闸门」）
                    waited = 0.0
                    while not type(self).approvals and waited < 3.0:
                        time.sleep(0.05)
                        waited += 0.05
                    if type(self).approvals:
                        self.wfile.write(
                            f"data: {json.dumps({'event': 'approval.resolved'})}\n\n".encode())
                        self.wfile.flush()
                        self.wfile.write(
                            f"data: {json.dumps({'event': 'run.completed', 'output': '好了。'}, ensure_ascii=False)}\n\n".encode())
                        self.wfile.flush()
                    return
            return
        if "/v1/runs/" in self.path:
            status = "waiting_for_approval" if not type(self).approvals else "completed"
            self._json({"status": status})
            return
        self.send_error(404)

    def _json(self, payload: dict):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        return None


class DelegateCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHermes)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.host = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeHermes.script = {}
        FakeHermes.runs = []
        FakeHermes.approvals = []
        FakeHermes.counter = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = pathlib.Path(self.tmp.name) / "out"
        self.out.mkdir()
        self.gateway = HermesGateway(self.host, "test-key", timeout=20, poll_s=0.05)
        self.service = DelegateService(self.gateway, artifact_dirs=[str(self.out)],
                                       wait_s=0.6, deadline=20)

    def script_run(self, frames, *, run_id="run-1"):
        FakeHermes.script[run_id] = frames

    def done_frames(self, text: str):
        return [{"event": "message.delta", "delta": text},
                {"event": "run.completed", "output": text}]


class TestOwnerOnly(DelegateCase):
    def test_guest_is_refused(self):
        result = self.service.run(task="把檔案改名", principal="guest-x", thread_id="th-1")
        self.assertEqual(result.refused, "work_requires_owner")
        self.assertFalse(result.ok)
        self.assertEqual(FakeHermes.runs, [], "guest 一步都不該跑到 Hermes")

    def test_owner_is_accepted(self):
        self.script_run(self.done_frames("做好了。"))
        result = self.service.run(task="把檔案改名", principal="owner", thread_id="th-1")
        self.assertTrue(result.run_id)
        self.assertEqual(len(FakeHermes.runs), 1)


class TestToolSurface(unittest.TestCase):
    def test_delegate_tool_hidden_in_chat_mode(self):
        from agent.brain.runtime.tools import DelegateTool, ToolRegistry
        registry = ToolRegistry().register(DelegateTool(delegate=None))
        names = lambda mode, owner, caps=(): [s["function"]["name"] for s in
                                             registry.schemas(mode=mode, is_owner=owner,
                                                              capabilities=caps)]
        self.assertNotIn("delegate", names("chat", True, {"delegate"}))
        self.assertNotIn("delegate", names("work", False, {"delegate"}))
        # 沒有 capability（＝user.md 沒放開）也看不到
        self.assertNotIn("delegate", names("work", True))
        self.assertIn("delegate", names("work", True, {"delegate"}))

    def test_registry_refuses_disallowed_call(self):
        from agent.brain.runtime.tools import (DelegateTool, ToolContext,
                                                  ToolRegistry)
        registry = ToolRegistry().register(DelegateTool(delegate=None))
        ctx = ToolContext(entity="mira", principal="alice", thread_id="th",
                          source="phone:alice", voice=None, mode="work", is_owner=False,
                          capabilities=frozenset({"delegate"}))
        result = registry.run("delegate", {"task": "x"}, ctx)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("tool_not_allowed"))

    def test_owner_without_profile_permission_is_also_refused(self):
        """主人但 user.md 沒放開 ⇒ 一樣不給（檔是策略來源，fail-closed）。"""
        from agent.brain.runtime.tools import (DelegateTool, ToolContext,
                                                  ToolRegistry)
        registry = ToolRegistry().register(DelegateTool(delegate=None))
        ctx = ToolContext(entity="mira", principal="owner", thread_id="th",
                          source="phone:owner", voice=None, mode="work", is_owner=True)
        result = registry.run("delegate", {"task": "x"}, ctx)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("tool_not_allowed"))


class TestPlaceholderAndTail(DelegateCase):
    def test_placeholder_replaced_and_tail_appended(self):
        self.script_run(self.done_frames("了解。"))
        self.service.run(task="做一張圖，存成 {{OUT}}.png", principal="owner",
                         thread_id="th-9")
        sent = FakeHermes.runs[0]["body"]["input"]
        self.assertNotIn("{{OUT}}", sent)
        self.assertIn("ba-th-9-", sent)
        self.assertIn("【委任の約束】", sent)

    def test_explicit_out_name_is_used(self):
        self.script_run(self.done_frames("了解。"))
        self.service.run(task="產出 {{OUT}}.png", principal="owner", thread_id="th-9",
                         out_name="card-001")
        self.assertIn("card-001.png", FakeHermes.runs[0]["body"]["input"])


class TestPathStripping(DelegateCase):
    def test_paths_are_stripped_from_brain_copy(self):
        self.script_run(self.done_frames("完成了，保存在 /vol1/@appdata/trim.hermes/out/a.png 裡。"))
        frames: list[tuple[str, dict]] = []
        result = self.service.run(task="存一張圖", principal="owner", thread_id="th-2",
                                  emit=lambda kind, payload: frames.append((kind, payload)))
        self.assertNotIn("/vol1/", result.text)
        self.assertIn("完成了", result.text)

    def test_strip_paths_unit(self):
        self.assertEqual(strip_paths("存在 /home/developer/y.png 了"), "存在 （端末の中） 了")
        self.assertEqual(strip_paths("沒有路徑"), "沒有路徑")


class TestReceipt(DelegateCase):
    def test_delivered_when_artifact_appears(self):
        stem = "ba-th3-fixed"
        (self.out / f"{stem}.png").write_bytes(b"png")
        self.script_run(self.done_frames("完成了。"))
        result = self.service.run(task="產出 {{OUT}}.png", principal="owner",
                                  thread_id="th-3", out_name=stem)
        self.assertEqual(result.receipt, RECEIPT_DELIVERED)
        self.assertTrue(result.artifacts)
        self.assertIn("【交付の控え】", result.text)

    def test_missing_when_no_artifact(self):
        self.script_run(self.done_frames("完成了。"))
        result = self.service.run(task="產出 {{OUT}}.png", principal="owner",
                                  thread_id="th-4", out_name="ba-th4-none")
        self.assertEqual(result.receipt, RECEIPT_MISSING)
        self.assertFalse(result.artifacts)

    def test_artifact_frame_reaches_the_app_with_path(self):
        stem = "ba-th5-fixed"
        (self.out / f"{stem}.png").write_bytes(b"png")
        self.script_run(self.done_frames("完成了。"))
        frames: list[tuple[str, dict]] = []
        self.service.run(task="產出 {{OUT}}.png", principal="owner", thread_id="th-5",
                         out_name=stem, emit=lambda kind, payload: frames.append((kind, payload)))
        artifacts = [payload for kind, payload in frames if kind == "artifact"]
        self.assertTrue(artifacts)
        self.assertIn("path", artifacts[0])
        self.assertTrue(artifacts[0]["path"].endswith(".png"))


class TestToolFrames(unittest.TestCase):
    def test_artifact_frames_are_forwarded_with_path(self):
        """產物要推給前端（帶路徑），模型那份才剝掉路徑。"""
        from agent.brain.runtime.delegate import DelegateResult
        from agent.brain.runtime.tools import DelegateTool, ToolContext

        class FakeService:
            def run(self, **_kwargs):
                return DelegateResult(ok=True, text="做好了", receipt="【交付回执】已交付",
                                      artifacts=["/tmp/out/a.png"])

        tool = DelegateTool(FakeService())
        ctx = ToolContext(entity="mira", principal="owner", thread_id="th",
                          source="phone:owner", voice=None, mode="work", is_owner=True)
        result = tool.run(ctx, {"task": "做圖"})
        kinds = [frame["kind"] for frame in result.frames]
        self.assertIn("artifact", kinds)
        artifact = next(frame for frame in result.frames if frame["kind"] == "artifact")
        self.assertEqual(artifact["payload"]["path"], "/tmp/out/a.png")
        self.assertEqual(artifact["payload"]["name"], "a.png")

    def test_guest_call_is_refused_by_the_tool_too(self):
        from agent.brain.runtime.delegate import DelegateResult
        from agent.brain.runtime.tools import DelegateTool, ToolContext

        class Refusing:
            def run(self, **kwargs):
                return DelegateResult(ok=False, refused="work_requires_owner")

        tool = DelegateTool(Refusing())
        ctx = ToolContext(entity="mira", principal="guest-x", thread_id="th",
                          source="phone:guest", voice=None, mode="work", is_owner=False)
        result = tool.run(ctx, {"task": "做圖"})
        self.assertFalse(result.ok)
        self.assertIn("work_requires_owner", result.error)


class TestDeduplication(DelegateCase):
    def test_same_task_twice_is_skipped(self):
        self.script_run(self.done_frames("完成了。"))
        first = self.service.run(task="整理桌面", principal="owner", thread_id="th-6")
        second = self.service.run(task="整理桌面", principal="owner", thread_id="th-6")
        self.assertTrue(first.run_id)
        self.assertEqual(second.refused, "duplicate_task")
        self.assertEqual(len(FakeHermes.runs), 1, "同一件事不該跑兩次")

    def test_forget_allows_again(self):
        self.script_run(self.done_frames("完成了。"))
        self.service.run(task="整理桌面", principal="owner", thread_id="th-6")
        self.service.forget("th-6")
        again = self.service.run(task="整理桌面", principal="owner", thread_id="th-6")
        self.assertTrue(again.run_id)
        self.assertEqual(len(FakeHermes.runs), 2)


class TestApprovalAndTimeout(DelegateCase):
    def test_approval_is_answered_automatically(self):
        self.script_run([{"event": "approval.required", "prompt": "允許寫檔？"},
                         {"event": "run.completed", "output": "好了。"}])
        result = self.service.run(task="寫一個檔", principal="owner", thread_id="th-7")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not FakeHermes.approvals:
            time.sleep(0.02)
        self.assertTrue(FakeHermes.approvals, "無人值守的 run 必須被代答，否則會卡住")
        self.assertEqual(FakeHermes.approvals[0]["choice"], "allow")
        self.assertTrue(result.ok)

    def test_timeout_reports_instead_of_hanging(self):
        """上游卡住不發事件時也要收手（只靠「收到事件才檢查 deadline」會被靜默卡死）。"""
        self.service.deadline = 1.0
        self.script_run([{"event": "__hang__", "seconds": 8}])
        started = time.monotonic()
        result = self.service.run(task="很久的活", principal="owner", thread_id="th-8")
        self.assertLess(time.monotonic() - started, 6, "不該等滿 8 秒")
        self.assertIn("timeout", result.error.lower(), result.error)



class TestDelegateIsNeutral(unittest.TestCase):
    """owner 2026-11-11：「不需要暴露给 blueagentnext 这边，委任对象是什么 agent」。"""

    def test_schema_names_no_agent(self):
        from agent.brain.runtime.tools.delegate import DelegateTool
        import json
        text = json.dumps(DelegateTool.schema, ensure_ascii=False)
        self.assertEqual(DelegateTool.name, "delegate")
        self.assertNotIn("Hermes", text)
        self.assertNotIn("hermes", text)

    def test_old_names_still_resolve(self):
        from agent.brain.runtime.tools import ToolRegistry
        from agent.brain.runtime.tools.delegate import DelegateTool
        registry = ToolRegistry().register(DelegateTool(delegate=None))
        for old in ("delegate.hermes", "delegate_hermes", "delegate"):
            self.assertIs(registry.get(old).__class__, DelegateTool, old)

if __name__ == "__main__":
    unittest.main(verbosity=2)
