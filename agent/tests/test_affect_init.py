#!/usr/bin/env python3
"""AEM `AffectService.ensure()` 的初始化競態（2026-10-27）。

心跳執行緒與回話執行緒會同時問同一個角色的 state：兩邊都先看到 `NOT_INITIALIZED`，
一邊 `init()` 成功、另一邊就被 `ALREADY_INITIALIZED` 打回來。那**不是錯**——
以前沒接住，`note_message()` 會回一個沒有 `event_id` 的錯誤 dict，
測試於是偶發 `KeyError: 'event_id'`（`test_mood_in_prompt` / `test_proactive` 都中過）。
"""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from agent.brain.runtime.inner.affect import AffectService
from agent.brain.vendor import aem_v2


class TestEnsureInitRace(unittest.TestCase):
    def setUp(self):
        self.service = AffectService(tempfile.mkdtemp(prefix="ba-affect-race-"), enabled=True)
        self.service.ensure("mira")                     # 先正常建好一份 state

    def test_already_initialized_is_tolerated(self):
        """這次 read 仍看到 NOT_INITIALIZED，但 init 時檔案已經被別的執行緒建好 ⇒ 重讀就好。"""
        inner = self.service._store("mira")
        reads = {"count": 0}

        class Racing:
            def read(self):
                reads["count"] += 1
                if reads["count"] == 1:
                    raise aem_v2.AEMError("NOT_INITIALIZED", 4)
                return inner.read()

            def init(self, *_args, **_kwargs):
                raise aem_v2.AEMError("ALREADY_INITIALIZED", 3)

            def __getattr__(self, name):
                return getattr(inner, name)

        with mock.patch.object(self.service, "_store", lambda entity: Racing()):
            state = self.service.ensure("mira")
        self.assertIn("affect", state, "重讀要拿到真的 state")
        self.assertEqual(reads["count"], 2, "第一次 read 失敗、init 撞到已存在、再讀一次")

    def test_note_message_still_works_after_the_race(self):
        """走完整條路：競態被接住之後 `note_message()` 仍要回 ok + event_id。"""
        inner = self.service._store("mira")
        reads = {"count": 0}

        class Racing:
            def read(self):
                reads["count"] += 1
                if reads["count"] == 1:
                    raise aem_v2.AEMError("NOT_INITIALIZED", 4)
                return inner.read()

            def init(self, *_args, **_kwargs):
                raise aem_v2.AEMError("ALREADY_INITIALIZED", 3)

            def __getattr__(self, name):
                return getattr(inner, name)

        with mock.patch.object(self.service, "_store", lambda entity: Racing()):
            noted = self.service.note_message("mira", "ねえ、聞いて", speaker="owner")
        self.assertTrue(noted.get("ok"), noted)
        self.assertTrue(noted.get("event_id"), noted)

    def test_other_errors_still_raise(self):
        """別種 AEM 錯誤不可以被吞掉（fail-loud，不要靜靜變成空狀態）。"""
        class Broken:
            def read(self):
                raise aem_v2.AEMError("LOCK_TIMEOUT", 5)

            def __getattr__(self, name):
                raise AttributeError(name)

        with mock.patch.object(self.service, "_store", lambda entity: Broken()):
            with self.assertRaises(aem_v2.AEMError):
                self.service.ensure("mira")


if __name__ == "__main__":
    unittest.main()
