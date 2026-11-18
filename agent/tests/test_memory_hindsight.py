#!/usr/bin/env python3
"""記憶框架＝與現役 blueagent 相同（Hindsight bank）＋ 本地原文/ACL 層。

判據：
  ① bank 映射照設計書 §6.3：shared=`blueagent-<entity>`、private=`…-u-<principal>`、self=`…-self`；
  ② 寫入推到對的 bank（`async=true`），且本地 jsonl/索引同時有；
  ③ **ACL 由本地權威判定**：Hindsight 就算把別人的私密撈回來，也進不了 prompt；
  ④ Hindsight 掛掉 ⇒ 自動退回本地 SQLite 檢索（記憶檢索失敗不擋對話）；
  ⑤ 本地沒有的文字（不是我們寫的）不被採用。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from demo_common import *  # noqa: F401,F403

from agent.adapter.memory_hindsight import BANK_OF_LAYER, HindsightMemory
from agent.brain.memory.fact_store import FactStore
from agent.brain.domain.thread import MemoryScope

NAMES = {"owner": "主人", "alice": "Alice"}


def scope(principal: str = "owner", entity: str = "mira") -> MemoryScope:
    return MemoryScope(principal=principal, entity=entity, scene="together",
                       thread_id=f"th-{principal}")


class FakeHindsight(BaseHTTPRequestHandler):
    """假的 Hindsight：記請求、回固定 recall 結果（可注入）。"""

    pushes: list[dict] = []
    recalls: list[dict] = []
    results: list[dict] = []
    fail = False
    fail_recall = False

    def do_POST(self):                                     # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        body = json.loads(self.rfile.read(length).decode() or "{}")
        if type(self).fail:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b'{"error":"down"}')
            return
        if self.path.endswith("/memories/recall"):
            if type(self).fail_recall:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'{"error":"down"}')
                return
            type(self).recalls.append({"path": self.path, "body": body})
            payload = {"results": list(type(self).results)}
        else:
            type(self).pushes.append({"path": self.path, "body": body})
            payload = {"ok": True}
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):                                      # noqa: N802
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"healthy"}')
            return
        self.send_error(404)

    def log_message(self, *_args):
        return None


class HindsightCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHindsight)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.host = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeHindsight.pushes = []
        FakeHindsight.recalls = []
        FakeHindsight.results = []
        FakeHindsight.fail = False
        FakeHindsight.fail_recall = False
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.memory = HindsightMemory(root / "facts.db", host=self.host,
                                      store=FactStore(root / "facts.jsonl"),
                                      names=NAMES, timeout=2.0, seed=False)

    def bank_names(self) -> set[str]:
        return {push["path"].split("/banks/")[1].split("/")[0]
                for push in FakeHindsight.pushes}


class TestBankMapping(HindsightCase):
    def test_layer_to_bank(self):
        self.assertEqual(self.memory.bank_for(scope(), "world"), "blueagent-mira")
        self.assertEqual(self.memory.bank_for(scope(), "circle"), "blueagent-mira")
        self.assertEqual(self.memory.bank_for(scope(), "private"),
                         "blueagent-mira-u-owner")
        self.assertEqual(self.memory.bank_for(scope(), "pair"),
                         "blueagent-mira-u-owner")
        self.assertEqual(self.memory.bank_for(scope(), "self"), "blueagent-mira-self")

    def test_table_matches_design(self):
        self.assertEqual(BANK_OF_LAYER["world"], "shared")
        self.assertEqual(BANK_OF_LAYER["pair"], "private")
        self.assertEqual(BANK_OF_LAYER["self"], "self")

    def test_self_scope_goes_to_self_bank(self):
        own = MemoryScope(principal="", entity="mira", scene="trpg",
                          thread_id="th-self", self_owned=True)
        self.assertEqual(self.memory.bank_for(own, "self"), "blueagent-mira-self")


class TestPush(HindsightCase):
    def test_world_fact_goes_to_shared_bank(self):
        self.memory.add("主人怕冷，空调开 26 度。", layer="world", entity="mira")
        self.assertEqual(self.bank_names(), {"blueagent-mira"})
        push = FakeHindsight.pushes[0]
        self.assertTrue(push["body"]["async"])
        self.assertIn("layer=world", push["body"]["items"][0]["context"])

    def test_private_fact_goes_to_principal_bank(self):
        self.memory.add("主人养了只叫团子的猫。", layer="private",
                        audience={"mira", "owner"}, principal="owner", entity="mira")
        self.assertEqual(self.bank_names(), {"blueagent-mira-u-owner"})

    def test_local_storage_still_written(self):
        self.memory.add("窗边有薄荷。", layer="world", entity="mira")
        self.assertEqual(self.memory.local.count(), 1)
        self.assertIsNotNone(self.memory.local.fact_by_text("窗边有薄荷。"))


class TestRecallKeepsACL(HindsightCase):
    def test_hindsight_hit_is_filtered_by_local_audience(self):
        self.memory.add("主人养了只叫团子的猫。", layer="private",
                        audience={"mira", "owner"}, principal="owner", entity="mira")
        # Hindsight 把「主人的私密」還給 Alice 的查詢——本地必須擋掉
        FakeHindsight.results = [{"text": "主人养了只叫团子的猫。", "scores": {"final": 1.0}}]
        alice = MemoryScope(principal="alice", entity="mira", scene="together",
                            thread_id="th-alice")
        texts = [fact.text for fact in self.memory.recall_detail(
            alice, "猫", limit=4, participants=["alice"])]
        self.assertFalse(any("团子" in text for text in texts), texts)

    def test_hindsight_hit_for_the_right_person_is_used(self):
        self.memory.add("主人养了只叫团子的猫。", layer="private",
                        audience={"mira", "owner"}, principal="owner", entity="mira")
        FakeHindsight.results = [{"text": "主人养了只叫团子的猫。", "scores": {"final": 1.0}}]
        texts = [fact.text for fact in self.memory.recall_detail(
            scope("owner"), "猫", limit=4, participants=["owner"])]
        self.assertTrue(any("团子" in text for text in texts), texts)

    def test_paraphrased_hit_still_maps_and_keeps_acl(self):
        """Hindsight 會改寫原句（抽取層）⇒ 用「最像的一條」核對，ACL 仍由本地判。"""
        self.memory.add("主人喜欢薄荷（probe）。", layer="private",
                        audience={"mira", "owner"}, principal="owner", entity="mira")
        FakeHindsight.results = [{"text": "主人喜欢薄荷。", "scores": {"final": 0.9}}]
        # 主人問：可以用
        texts = [fact.text for fact in self.memory.recall_detail(
            scope("owner"), "薄荷", limit=4, participants=["owner"])]
        self.assertTrue(any("薄荷" in text for text in texts), texts)
        # Alice 問：改寫版也要被擋掉
        alice = MemoryScope(principal="alice", entity="mira", scene="together",
                            thread_id="th-alice")
        alice_texts = [fact.text for fact in self.memory.recall_detail(
            alice, "薄荷", limit=4, participants=["alice"])]
        self.assertFalse(any("薄荷（probe）" in text for text in alice_texts), alice_texts)

    def test_unknown_text_is_ignored(self):
        self.memory.add("窗边有薄荷。", layer="world", entity="mira")
        FakeHindsight.results = [{"text": "這條不是我們寫的", "scores": {"final": 1.0}}]
        texts = [fact.text for fact in self.memory.recall_detail(
            scope(), "什麼", limit=4, participants=["owner"])]
        self.assertNotIn("這條不是我們寫的", texts, "本地沒有原文的條目不能被採用")

    def test_recall_asks_shared_private_and_self_banks(self):
        self.memory.add("窗边有薄荷。", layer="world", entity="mira")
        self.memory.recall_detail(scope("owner"), "薄荷", limit=4, participants=["owner"])
        asked = {call["path"].split("/banks/")[1].split("/")[0]
                 for call in FakeHindsight.recalls}
        self.assertEqual(asked, {"blueagent-mira", "blueagent-mira-u-owner",
                                 "blueagent-mira-self"})


class TestFailOpen(HindsightCase):
    def test_hindsight_down_falls_back_to_local(self):
        self.memory.add("窗边有薄荷。", layer="world", entity="mira")
        FakeHindsight.fail_recall = True
        texts = [fact.text for fact in self.memory.recall_detail(
            scope(), "薄荷", limit=4, participants=["owner"])]
        self.assertTrue(any("薄荷" in text for text in texts), "挂了也要能用本地检索")
        self.assertTrue(self.memory.last_error)

    def test_push_failure_does_not_raise(self):
        FakeHindsight.fail = True
        fact = self.memory.add("窗边有薄荷。", layer="world", entity="mira")
        self.assertEqual(fact.text, "窗边有薄荷。")
        self.assertEqual(self.memory.local.count(), 1)

    def test_health_reports_host(self):
        health = self.memory.health()
        self.assertTrue(health["ok"])
        self.assertEqual(health["prefix"], "blueagent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
