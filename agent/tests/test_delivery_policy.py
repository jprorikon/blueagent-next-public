#!/usr/bin/env python3
"""投遞規則（owner 2026-10-22）：
  ① 寂寞／親密的話**只走私聊**，不貼群（「寂寞发话最好还是私聊或者将来手机 app」）；
  ② TRPG 匯報**不投 Discord**，預設只落 `data/logs/trpg-reports.jsonl` ＋她的私記。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.delivery.delivery import DiscordChannel
from agent.brain.runtime.trpg_reports import append_report, report_path, tail


class FakeSource:
    def __init__(self):
        self.group: list[str] = []
        self.dm: list[tuple[str, str]] = []
        self.config = type("Cfg", (), {"channel_id": "c1"})()

    def can_dm(self, principal):
        return True

    def send_text(self, text):
        self.group.append(text)
        return True

    def send_dm(self, principal, text):
        self.dm.append((principal, text))
        return True


class TestLonelyGoesPrivate(unittest.TestCase):
    def test_lonely_message_skips_the_group(self):
        source = FakeSource()
        channel = DiscordChannel(source)
        ok = channel.send("owner", {"text": "ねえ、ちょっと寂しいんだけど", "urgency": "lonely"})
        self.assertTrue(ok)
        self.assertEqual(source.group, [], "寂寞的話不能貼群")
        self.assertEqual(source.dm[0][1], "ねえ、ちょっと寂しいんだけど")

    def test_public_message_still_goes_to_group(self):
        source = FakeSource()
        channel = DiscordChannel(source)
        channel.send("owner", {"text": "今日はいい天気だね"})
        self.assertEqual(source.group, ["今日はいい天気だね"], "普通的話照舊先貼群")

    def test_private_flag_also_skips_group(self):
        source = FakeSource()
        channel = DiscordChannel(source)
        channel.send("owner", {"text": "内緒の話", "visibility": "private"})
        self.assertEqual(source.group, [])
        self.assertEqual(len(source.dm), 1)


class TestTrpgReportsStayLocal(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())

    def test_report_is_written_locally(self):
        ok = append_report(self.root, entity="mira", text="灯の向こうに気配がひとつ",
                           table="smoke_x", seat="p1", round_no=3)
        self.assertTrue(ok)
        path = report_path(self.root)
        self.assertTrue(path.is_file())
        rows = tail(self.root)
        self.assertEqual(rows[0]["round"], 3)
        self.assertEqual(rows[0]["table"], "smoke_x")
        self.assertIn("気配", rows[0]["text"])

    def test_empty_text_is_not_written(self):
        self.assertFalse(append_report(self.root, entity="mira", text="   "))

    def test_tail_is_bounded_and_ordered(self):
        for index in range(5):
            append_report(self.root, entity="mira", text=f"第{index}輪", round_no=index)
        rows = tail(self.root, limit=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["round"], 4)

    def test_bad_lines_do_not_break_tail(self):
        path = report_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"round":1}\nnot json\n', encoding="utf-8")
        rows = tail(self.root)
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
