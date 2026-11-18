#!/usr/bin/env python3
"""收幀路徑不准被 brain I/O 卡住（2026-11-08 通話 7 秒自動掛斷的回歸）。

病灶：`AppVoiceService.feed()` 在 `session.lock` 裡同步 `bridge.call("interrupt")`。
手機每 100 ms 一幀、串行等回包 ⇒ 一秒只送得出 2 幀 ⇒ 積壓超過客戶端 50 幀上限 ⇒
`NextCallView` 直接 `Finish("通話が切れました")`（裝置日誌 23:07:07.997 實證）。
"""

from __future__ import annotations

import base64
import struct
import time
import unittest
from types import SimpleNamespace

from app.server.voice import RATE, AppVoiceService


def _pcm(ms: int, amplitude: int = 9000) -> bytes:
    count = RATE * ms // 1000
    return struct.pack("<" + "h" * count, *([amplitude] * count))


def _frame(ms: int = 100, amplitude: int = 9000) -> str:
    return base64.b64encode(_pcm(ms, amplitude)).decode("ascii")


class SlowBridge:
    """打斷要 2 秒才回來；其餘立刻回。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def call(self, action: str, payload: dict) -> dict:
        self.calls.append(action)
        if action == "interrupt":
            time.sleep(2.0)
        return {"ok": True, "events": []}


class FeedNotBlockedCase(unittest.TestCase):
    def setUp(self):
        self.account = SimpleNamespace(id="acct_owner", kind="owner", display="主人",
                                      visible_entities=None, legacy_principal="owner",
                                      actor=lambda: None)
        self.target = SimpleNamespace(id="mira", kind="entity", display="ミラ")
        self.accounts = SimpleNamespace(
            get=lambda account_id: self.target if account_id == "mira" else self.account)
        self.bridge = SlowBridge()
        self.service = AppVoiceService(self.accounts, self.bridge)
        opened = self.service.open(self.account, "mira")
        self.call_id = opened["session"]["id"]

    def test_speech_onset_returns_before_brain_interrupt_finishes(self):
        seq = 1
        # 先送靜音把噪聲底定下來，再連續送有聲幀，直到切句判「開口」（epoch 增加）
        for _ in range(6):
            self.service.feed(SimpleNamespace(id="acct_owner"), self.call_id, seq, _frame(100, 0))
            seq += 1
        slowest = 0.0
        started = False
        for _ in range(30):
            moment = time.monotonic()
            result = self.service.feed(SimpleNamespace(id="acct_owner"), self.call_id, seq,
                                       _frame(100, 9000))
            slowest = max(slowest, time.monotonic() - moment)
            seq += 1
            if int(result.get("epoch") or 0) >= 1:
                started = True
                break
        self.assertTrue(started, "30 幀內該判到開口（切句閘沒起來 ⇒ 測試素材不對）")
        self.assertLess(slowest, 0.5, f"收幀被 brain 打斷卡住 {slowest:.2f}s")
        time.sleep(0.1)
        self.assertIn("interrupt", self.bridge.calls, "打斷該被非同步送出")


if __name__ == "__main__":
    unittest.main()
