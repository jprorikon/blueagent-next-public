#!/usr/bin/env python3
"""Firecrawl 起始 URL 只准公網＋ notes.read 只讀當天筆記檔。

判據：
  ① 回環／私網／鏈路本地／tailnet（CGNAT）／IPv6 本地／映射位址／解析不到 ⇒ 擋；
     公網 ⇒ 放；非 http(s) ⇒ 擋；
  ② `notes.read`：只讀最近 `days` 天的 `YYYY-MM-DD.md`；日記 `reflection-*.md` 一律不讀。
"""

from __future__ import annotations

import datetime
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from demo_common import *  # noqa: F401,F403

from agent.adapter import netguard
from agent.brain.runtime.tools import NotesReadTool, ToolContext

PUBLIC_IP = "93.184.216.34"


def _fake_resolve(table):
    return lambda host: list(table.get(str(host).lower(), []))


class TestClassification(unittest.TestCase):
    def test_private_and_local_addresses_are_blocked(self):
        for url in ("http://127.0.0.1/", "http://192.168.50.29:8891/health", "http://10.0.0.1/",
                    "http://172.16.5.5/", "http://169.254.169.254/latest/meta-data/",
                    "http://100.64.0.1:3000/", "http://[::1]/", "http://[fc00::1]/",
                    "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/"):
            self.assertTrue(netguard.blocked_reason(url), url)

    def test_hostnames_are_judged_by_what_they_resolve_to(self):
        table = {"localhost.local": ["192.168.50.29"], "example.org": [PUBLIC_IP],
                 "mixed.example": [PUBLIC_IP, "10.0.0.8"]}
        with mock.patch.object(netguard, "_resolve", _fake_resolve(table)):
            self.assertEqual(netguard.blocked_reason("http://localhost.local:8891/"),
                             "private_address")
            self.assertEqual(netguard.blocked_reason("https://mixed.example/"), "private_address",
                             "任何一個位址在內網就擋")
            self.assertEqual(netguard.blocked_reason("https://nowhere.invalid/"), "unresolvable")
            self.assertEqual(netguard.blocked_reason("https://example.org/post"), "")

    def test_only_http_schemes(self):
        for url in ("file:///etc/passwd", "ftp://example.org/", "gopher://x/", "javascript:1"):
            self.assertEqual(netguard.blocked_reason(url), "scheme", url)
        self.assertEqual(netguard.blocked_reason(f"https://{PUBLIC_IP}/"), "")


class TestNotesReadOnlyDatedNotes(unittest.TestCase):
    def test_reads_recent_notes_never_diaries(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="ba-notes-"))
        folder = root / "mira"
        folder.mkdir(parents=True, exist_ok=True)
        today = datetime.date(2026, 10, 25)
        for back in (0, 1, 5):
            day = (today - datetime.timedelta(days=back)).isoformat()
            (folder / f"{day}.md").write_text(f"# {day}\n\n## 10:00 「自習」／判定\nnote-{back}\n",
                                              encoding="utf-8")
            # 日記：檔名排在筆記後面，正文還帶 `## ` 標題（以前會被當成筆記讀出來）
            (folder / f"reflection-{day}.md").write_text(
                f"# {day} 日記\n\n## 主人のこと\n主人の健康診断の結果が悪かった\n",
                encoding="utf-8")
        clock = lambda: time.mktime(today.timetuple()) + 12 * 3600
        ctx = ToolContext(entity="mira", principal="owner", thread_id="self",
                          source="self", voice=None, mode="work")
        result = NotesReadTool(root, clock=clock).run(ctx, {"days": 3, "limit": 12})
        summary = result.data["summary"]
        self.assertNotIn("健康診断", summary, "日記不是筆記")
        self.assertIn("note-0", summary)
        self.assertIn("note-1", summary)
        self.assertNotIn("note-5", summary, "超過 days 的不讀")
        self.assertEqual(result.data["count"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
