#!/usr/bin/env python3
"""C-17：Agent 只消费远程贴纸语义；旧文件端点按需缓存单张，故障不影响聊天。"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.adapter.app_assets import RemoteStickerCatalog, build_sticker_catalog
from app.client.assets.stickers import StickerCatalog

PNG = b"\x89PNG\r\n\x1a\nclient-sticker"


class FakeClient:
    instances = []
    fail = False
    hash_value = "abc123"

    def __init__(self, base_url, token, *, timeout):
        self.token = token
        self.calls = []
        self.downloads = []
        self.__class__.instances.append(self)

    def sticker_list(self, entity):
        self.calls.append(entity)
        if self.fail:
            raise RuntimeError("offline")
        return {"stickers": [{"id": "smile", "label": "にこっ", "meaning": "うれしい",
                              "hash": self.hash_value, "url": "https://ignored.invalid/file"},
                             {"id": "../bad", "meaning": "drop"}]}

    def read_sticker(self, entity, sticker_id):
        self.downloads.append((entity, sticker_id))
        if self.fail:
            raise RuntimeError("offline")
        return {"bytes": PNG, "mime": "image/png"}


class RemoteStickerCatalogCase(unittest.TestCase):
    def setUp(self):
        FakeClient.instances = []
        FakeClient.fail = False
        FakeClient.hash_value = "abc123"
        self.tmp = tempfile.TemporaryDirectory(prefix="ba-remote-stickers-")
        self.addCleanup(self.tmp.cleanup)
        self.now = [10.0]
        self.catalog = RemoteStickerCatalog(
            "http://app.invalid", {"aster": "secret"}, self.tmp.name,
            client_factory=FakeClient, clock=lambda: self.now[0], ttl=30)

    def test_semantics_cache_legacy_url_and_single_file_cache(self):
        rows = self.catalog.list("aster")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].meaning, "うれしい")
        self.assertEqual(rows[0].to_dict()["url"], "/api/stickers/aster/smile")
        self.assertEqual(self.catalog.hash_of(rows[0]), "abc123")
        self.assertEqual(self.catalog.list("aster"), rows)
        self.assertEqual(FakeClient.instances[0].calls, ["aster"])
        self.assertEqual(self.catalog.list("nori"), [])

        path = self.catalog.path_of(rows[0])
        self.assertEqual(path.suffix, ".png")
        self.assertEqual(path.read_bytes(), PNG)
        self.assertNotIn("secret", str(path))
        self.assertEqual(self.catalog.path_of(rows[0]), path)
        self.assertEqual(FakeClient.instances[0].downloads, [("aster", "smile")])

    def test_stale_cache_and_cold_failure_are_fail_open(self):
        rows = self.catalog.list("aster")
        self.now[0] = 100.0
        FakeClient.fail = True
        self.assertEqual(self.catalog.list("aster"), rows)
        self.assertIsNone(self.catalog.path_of(rows[0]))
        other = RemoteStickerCatalog("http://app.invalid", {"nori": "token"},
                                     self.tmp.name, client_factory=FakeClient)
        FakeClient.fail = True
        self.assertEqual(other.list("nori"), [])
        self.assertIsNone(other.get("nori", "smile"))

    def test_composition_prefers_remote_then_supports_local_development(self):
        remote = build_sticker_catalog(data_dir=self.tmp.name, env={
            "BLUEAGENT_APP_SERVER_URL": "http://app.invalid",
            "BLUEAGENT_APP_SERVER_TOKENS": "aster=secret"})
        self.assertIsInstance(remote, RemoteStickerCatalog)
        local = build_sticker_catalog(data_dir=self.tmp.name, env={})
        self.assertIsInstance(local, StickerCatalog)
        explicit = build_sticker_catalog(data_dir=self.tmp.name, env={
            "BLUEAGENT_APP_SERVER_URL": "http://app.invalid",
            "BLUEAGENT_APP_SERVER_TOKENS": "aster=secret",
            "BLUEAGENT_STICKER_DIR": self.tmp.name})
        self.assertIsInstance(explicit, StickerCatalog)
        self.assertEqual(explicit.root, pathlib.Path(self.tmp.name))

    def test_failed_refresh_backs_off_instead_of_waiting_the_timeout_every_call(self):
        """⑤ REVIEW 2026-11-07 #5：App Server 不通时，别每次 list() 都等满超时。"""
        rows = self.catalog.list("aster")
        self.now[0] = 100.0                      # TTL 过期
        FakeClient.fail = True
        self.assertEqual(self.catalog.list("aster"), rows)
        calls_after_first_failure = len(FakeClient.instances[0].calls)
        self.assertEqual(calls_after_first_failure, 2)
        self.now[0] = 110.0                      # 仍在 backoff 窗口里 ⇒ 直接给旧列表
        self.assertEqual(self.catalog.list("aster"), rows)
        self.assertEqual(len(FakeClient.instances[0].calls), calls_after_first_failure)
        self.now[0] = 100.0 + self.catalog.backoff + 1
        self.catalog.list("aster")                 # 退避结束才再试一次
        self.assertEqual(len(FakeClient.instances[0].calls), calls_after_first_failure + 1)

    def test_current_cache_file_is_reused_when_the_server_sends_no_hash(self):
        """⑨ 没有 hash 的贴纸也会落 `-current` 档；以前只写不查，每次都重下。"""
        FakeClient.hash_value = ""
        catalog = RemoteStickerCatalog("http://app.invalid", {"aster": "secret"},
                                       self.tmp.name, client_factory=FakeClient,
                                       clock=lambda: self.now[0])
        rows = catalog.list("aster")
        first = catalog.path_of(rows[0])
        self.assertTrue(first.is_file())
        self.assertIn("-current", first.name)
        self.assertEqual(catalog.path_of(rows[0]), first)
        self.assertEqual(FakeClient.instances[-1].downloads, [("aster", "smile")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
