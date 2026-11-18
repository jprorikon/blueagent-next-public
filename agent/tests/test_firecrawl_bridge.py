#!/usr/bin/env python3
"""自架 Firecrawl 橋：全站共用的搜尋／抓頁入口與公網 URL 邊界。"""

from __future__ import annotations

import unittest
from unittest import mock

from demo_common import *  # noqa: F401,F403

from agent.tools import mcp_firecrawl_server as bridge


class TestFirecrawlBridge(unittest.TestCase):
    def test_url_tools_reject_private_target_before_post(self):
        for name in ("firecrawl_scrape", "firecrawl_map", "firecrawl_crawl"):
            with self.subTest(name=name), \
                    mock.patch.object(bridge, "blocked_reason", return_value="private_address"), \
                    mock.patch.object(bridge, "_post") as post:
                result = bridge.run_tool(name, {"url": "http://192.168.50.29/admin"})
                self.assertTrue(result["isError"])
                self.assertIn("url_blocked:private_address", result["content"][0]["text"])
                post.assert_not_called()

    def test_empty_url_is_rejected_for_every_url_tool(self):
        for name in ("firecrawl_scrape", "firecrawl_map", "firecrawl_crawl"):
            with self.subTest(name=name), mock.patch.object(bridge, "_post") as post:
                result = bridge.run_tool(name, {})
                self.assertTrue(result["isError"])
                post.assert_not_called()

    def test_public_scrape_returns_markdown_and_metadata(self):
        payload = {"data": {"markdown": "# 本文\n実際の内容", "title": "題",
                            "url": "https://example.org/post"}}
        with mock.patch.object(bridge, "blocked_reason", return_value=""), \
                mock.patch.object(bridge, "_post", return_value=payload) as post:
            result = bridge.run_tool("firecrawl_scrape", {"url": "https://example.org/post"})
        self.assertNotIn("isError", result)
        self.assertEqual(result["content"][0]["text"], "# 本文\n実際の内容")
        self.assertIn("題", result["content"][1]["text"])
        post.assert_called_once_with("scrape", {"url": "https://example.org/post",
                                                "formats": ["markdown"], "onlyMainContent": True})

    def test_search_returns_real_rows_without_url_guard(self):
        payload = {"data": [{"title": "判定入門", "url": "https://example.org/rule",
                             "description": "目標値との比較"}]}
        with mock.patch.object(bridge, "blocked_reason",
                               side_effect=AssertionError("搜尋字串不是 URL")), \
                mock.patch.object(bridge, "_post", return_value=payload):
            result = bridge.run_tool("firecrawl_search", {"query": "TRPG 判定", "limit": 3})
        text = result["content"][0]["text"]
        self.assertIn("判定入門", text)
        self.assertIn("目標値との比較", text)
        self.assertNotIn("isError", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
