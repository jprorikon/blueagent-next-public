#!/usr/bin/env python3
"""「像打電話」Phase 1：批量上傳幀 ＋ 450 ms 斷句（2026-11-08）。

事故背景：手機每 100 ms 一幀、串行一幀一請求，慢回包（實測 450 ms/幀）⇒ 6.5 秒積壓 50 幀
⇒ 客戶端 50 幀保險絲把整通掛斷。修法：一次帶多幀（seq 仍連續）＋客戶端改丟舊幀不掛斷。
"""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import app_paths  # noqa: F401 - 直接运行时也建立仓库导入路径

from app.server.config import AppConfig
from app.server.http import make_server
from app.server.voice import EnergyCutter, RATE


class FakeBridge:
    """只回 ok；本檔驗的是收幀與 seq，不驗回合。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def call(self, action: str, payload: dict) -> dict:
        self.calls.append(action)
        return {"ok": True, "events": []}


def pcm(ms: int, amplitude: int) -> bytes:
    count = RATE * ms // 1000
    return struct.pack("<" + "h" * count, *([amplitude] * count))


def b64(ms: int = 100, amplitude: int = 8000) -> str:
    return base64.b64encode(pcm(ms, amplitude)).decode("ascii")


class CutterTuningCase(unittest.TestCase):
    def test_silence_gate_is_450ms(self):
        self.assertEqual(EnergyCutter().end_ms, 450)

    def test_450ms_silence_closes_the_sentence(self):
        cutter = EnergyCutter()
        for _ in range(10):                    # 1 s 静音定噪聲底
            cutter.feed(pcm(100, 0))
        events = []
        for _ in range(10):                    # 1 s 说话
            events.extend(cutter.feed(pcm(100, 9000)))
        self.assertIn("started", [kind for kind, _ in events])
        for _ in range(5):                     # 500 ms 静音 ⇒ 该收句（阈值 450）
            events.extend(cutter.feed(pcm(100, 0)))
        self.assertIn("utterance", [kind for kind, _ in events])


class BatchedFramesCase(unittest.TestCase):
    """一次 POST 带 4 帧：seq 必须连续推进，乱序仍拒绝。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-call-batch-")
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        accounts = root / "accounts.json"
        accounts.write_text(json.dumps({"schema_version": 1, "accounts": [
            {"id": "acct_owner", "kind": "owner", "display": "主人",
             "token_sha256": hashlib.sha256(b"owner-token").hexdigest()},
            {"id": "mira", "kind": "entity", "display": "ミラ",
             "token_sha256": hashlib.sha256(b"mira-token").hexdigest()},
        ]}), encoding="utf-8")
        self.server = make_server(AppConfig("127.0.0.1", 0, root / "data", accounts))
        # 通話開線需要向 brain 打一次 start；這裡用假 bridge，不打真的 agent。
        self.server.services.voice.bridge = FakeBridge()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, method, path, body=None, token="owner-token"):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, json.loads(raw) if raw else None

    def test_four_frames_in_one_post_advance_seq_by_four(self):
        status, opened = self.request("POST", "/v1/calls", {"entity": "mira"})
        self.assertEqual(status, 200, opened)
        call_id = opened["session"]["id"]
        frames = [{"seq": i, "audio": b64(100, 0)} for i in range(1, 5)]
        status, reply = self.request("POST", f"/v1/calls/{call_id}/frames", {"frames": frames})
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply["next_seq"], 5)
        # 下一批從 5 接（亂序會 409）
        status, bad = self.request("POST", f"/v1/calls/{call_id}/frames",
                                   {"frames": [{"seq": 9, "audio": b64(100, 0)}]})
        self.assertEqual(status, 409, bad)

    def test_single_frame_shape_still_works(self):
        status, opened = self.request("POST", "/v1/calls", {"entity": "mira"})
        call_id = opened["session"]["id"]
        status, reply = self.request("POST", f"/v1/calls/{call_id}/frames",
                                     {"seq": 1, "audio": b64(100, 0)})
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply["next_seq"], 2)


if __name__ == "__main__":
    unittest.main()
