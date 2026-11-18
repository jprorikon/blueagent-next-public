#!/usr/bin/env python3
"""睡著時私聊先放著（`agent/adapter/app_dm_sleep.py`，owner 2026-11-09）。

以前這個模組**一條測試都沒有**（PENDING §D.9 只剩真機驗）。這裡用假時鐘與假 sidecar
把所有分支釘住：睡著先放、醒了一次讀（每會話一輪）、同人 15 分鐘連傳 3 則叫醒、
睡了就不讀、讀失敗放回去下次再讀。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.adapter import app_dm_sleep  # noqa: E402


class FakeSidecar:
    """`AdapterSidecar` 的最小替身：按 suffix 存一份 JSON。"""

    def __init__(self) -> None:
        self.data: dict[str, dict] = {}

    def read(self, suffix: str):
        return self.data.get(suffix)

    def write(self, suffix: str, payload) -> None:
        self.data[suffix] = payload


class FakeClock:
    def __init__(self, asleep: bool) -> None:
        self.value = bool(asleep)
        self.asked: list[float] = []

    def asleep(self, now: float) -> bool:      # noqa: A003 - 模仿現役介面
        self.asked.append(float(now))
        return self.value


class FakeAdapter:
    def __init__(self, *, asleep: bool, now: float) -> None:
        self.entity = "nori"
        self.sidecar = FakeSidecar()
        self.names = {"owner": "オーナー", "aster": "アスター"}
        self.logs: list[str] = []
        self.opened: list[dict] = []
        self.fail = False
        self._now = float(now)
        self.clock = FakeClock(asleep)
        self.group_attention = type("Attn", (), {"circadian": {"nori": self.clock}})()

    def _clock(self) -> float:
        return self._now

    def log(self, message: str) -> None:
        self.logs.append(str(message))

    def _handle(self, event: dict) -> None:
        if self.fail:
            raise RuntimeError("offline")
        self.opened.append(event)


def message(text: str, *, at: float, who: str = "acct_owner", kind: str = "owner") -> dict:
    return {"id": "msg_1", "created_at": at,
            "author": {"id": who, "kind": kind}, "parts": [{"kind": "text", "text": text}]}


def event_for(text: str, *, at: float, conversation: str = "cnv_dm") -> dict:
    return {"cursor": "1", "type": "chat.message.created",
            "payload": {"conversation_id": conversation, "conversation_kind": "direct",
                        "message": message(text, at=at)}}


class DmSleepCase(unittest.TestCase):
    NOW = time.mktime(time.strptime("2026-11-15 03:10", "%Y-%m-%d %H:%M"))

    def setUp(self) -> None:
        self.adapter = FakeAdapter(asleep=True, now=self.NOW)

    def hold(self, text: str, *, at: float | None = None, conversation: str = "cnv_dm"):
        at = self.NOW if at is None else at
        event = event_for(text, at=at, conversation=conversation)
        held = app_dm_sleep.hold(self.adapter, event, conversation,
                                 message(text, at=at), {"id": "acct_owner", "kind": "owner"}, text)
        return held, event

    def test_asleep_holds_the_message(self):
        held, _event = self.hold("まだ起きてる?")
        self.assertTrue(held, "睡著 ⇒ 這一則先不處理")
        items = app_dm_sleep._load(self.adapter)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "まだ起きてる?")

    def test_awake_replies_normally(self):
        self.adapter.clock.value = False
        held, _event = self.hold("おはよう")
        self.assertFalse(held, "醒著 ⇒ 照舊回")
        self.assertEqual(app_dm_sleep._load(self.adapter), [])

    def test_three_messages_from_same_person_wakes_her(self):
        self.hold("ねえ", at=self.NOW)
        self.hold("ねえってば", at=self.NOW + 60)
        held, event = self.hold("起きて!", at=self.NOW + 120)
        self.assertFalse(held, "同人 15 分鐘連傳 3 則 ⇒ 當作被叫醒")
        note = event.get("_slept") or ""
        self.assertIn("何度も呼ばれて目が覚めた", note)
        self.assertIn("ねえってば", note, "先到的幾則要帶進這一輪")
        self.assertEqual(app_dm_sleep._load(self.adapter), [], "叫醒後佇列清空")

    def test_tick_while_asleep_reads_nothing(self):
        self.hold("おやすみ")
        self.assertEqual(app_dm_sleep.tick(self.adapter, self.NOW), 0)
        self.assertEqual(len(app_dm_sleep._load(self.adapter)), 1, "還睡著 ⇒ 留著")

    def test_tick_after_waking_opens_one_turn_per_conversation(self):
        self.hold("眠い?", at=self.NOW, conversation="cnv_a")
        self.hold("ねえ", at=self.NOW + 30, conversation="cnv_a")
        self.hold("おはよう", at=self.NOW + 40, conversation="cnv_b")
        self.adapter.clock.value = False
        opened = app_dm_sleep.tick(self.adapter, self.NOW + 3600)
        self.assertEqual(opened, 2, "每個會話只開一輪")
        self.assertEqual(len(self.adapter.opened), 2)
        note = self.adapter.opened[0]["_slept"]
        self.assertIn("眠っていた間に届いていた", note)
        self.assertIn("眠い?", note, "先到的幾則要放進 [[room]]")
        self.assertIn("最後の一言", note)
        self.assertEqual(app_dm_sleep._load(self.adapter), [])

    def test_failed_turn_is_queued_again(self):
        self.hold("これ読んで", at=self.NOW)
        self.adapter.clock.value = False
        self.adapter.fail = True
        self.assertEqual(app_dm_sleep.tick(self.adapter, self.NOW + 3600), 0)
        self.assertEqual(len(app_dm_sleep._load(self.adapter)), 1, "讀失敗 ⇒ 下次再讀")

    def test_switch_off_keeps_old_behaviour(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"BLUEAGENT_DM_SLEEP_HOLD": "0"}):
            held, _event = self.hold("まだ起きてる?")
        self.assertFalse(held, "BLUEAGENT_DM_SLEEP_HOLD=0 ⇒ 回舊行為")
        self.assertEqual(app_dm_sleep._load(self.adapter), [])


if __name__ == "__main__":
    unittest.main()
