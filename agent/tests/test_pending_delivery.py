#!/usr/bin/env python3
"""未渡しの依頼（owner 2026-11-08）。

實測：主人 11-06 10:20 說「観測記録送ってくれる？チェックするから」，她那一輪回空；
之後四小時她反覆說「次、オーナーの番」——**她把自己的欠帳說成對方的欠帳**。
11-08 11:22 又說了一次，查下去發現兩件事：

  ① 心跳的「主人最後說過什麼」取的是「最近一條活線」，那條線是 aster×ミラ 互刷
     「……う」（說話人是 `mira`）⇒ 主人的話整段被過濾掉，她只剩自己上一句換詞。
  ② 主人要她交付的東西沒有任何地方記著 ⇒ 沒有東西提醒她「先交再催」。

判據：
  A. 交付請求用規則抓（不是模型），命中的原文照存；一般陳述不記
  B. 未交項會出現在回話 prompt（`[[todo]]`）與心跳 ground；真的交出（photo／artifact 幀）才清
  C. 主人的話**跨線**取（角色↔角色的活線不影響）
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

NOW = time.time() - 600

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from agent.brain.compose.heartbeat_compose import make_heartbeat_composer  # noqa: E402
from agent.brain.compose.turns import TurnService  # noqa: E402
from agent.brain.runtime.inner.pending_delivery import (  # noqa: E402
    PendingDeliveryStore, looks_like_delivery_request)
from agent.brain.runtime.threadstore import ThreadStore  # noqa: E402


def _turn(role, speaker, text, at):
    return SimpleNamespace(role=role, speaker=speaker, text=text, at=at, source="test")


class TestDeliveryRequestRule(unittest.TestCase):
    def test_the_real_request_is_matched(self):
        self.assertTrue(looks_like_delivery_request("観測記録送ってくれる？チェックするから"))
        self.assertTrue(looks_like_delivery_request("その表、見せて"))
        self.assertTrue(looks_like_delivery_request("できたら发我"))
        self.assertTrue(looks_like_delivery_request("出来たら確認するから"))

    def test_ordinary_talk_is_not_matched(self):
        self.assertEqual(looks_like_delivery_request("今日は寒いね"), "")
        self.assertEqual(looks_like_delivery_request("採点はどうなってる?"), "")
        self.assertEqual(looks_like_delivery_request("評価対象と評価基準について教えてくれないと口挟むことはできない"), "")
        # 長文は「一句請求」ではない（要東西的句子都很短）
        self.assertEqual(looks_like_delivery_request("送って" + "あ" * 300), "")


class TestPendingDeliveryStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PendingDeliveryStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_note_and_render(self):
        row = self.store.note_request("aster", "owner", "観測記録送ってくれる？チェックするから",
                                      at=NOW)
        self.assertIsNotNone(row)
        rows = self.store.items("aster")
        self.assertEqual(len(rows), 1)
        body = self.store.render("aster", principal="owner")
        self.assertIn("観測記録送ってくれる", body)

    def test_same_request_twice_is_one_debt(self):
        self.assertIsNotNone(self.store.note_request("aster", "owner", "その表、見せて", at=NOW))
        self.assertIsNone(self.store.note_request("aster", "owner", "その表、見せて", at=NOW+1))
        self.assertEqual(len(self.store.items("aster")), 1)

    def test_cleared_only_when_delivered(self):
        self.store.note_request("aster", "owner", "観測記録送ってくれる？", at=NOW)
        self.assertEqual(self.store.clear_delivered("aster", principal="owner"), 1)
        self.assertEqual(self.store.items("aster"), [])
        self.assertEqual(self.store.render("aster"), "")
        # 清過的紀錄留著（可回溯），只是不再進 prompt
        import json
        raw = json.loads((pathlib.Path(self.tmp.name) / "aster.json").read_text(encoding="utf-8"))
        self.assertTrue(raw["items"][0]["cleared_at"])
        self.assertEqual(raw["items"][0]["reason"], "delivered")

    def test_expiry_stops_nagging_but_keeps_record(self):
        store = PendingDeliveryStore(self.tmp.name, clock=lambda: 1_700_000_000.0, ttl_days=1)
        store.note_request("aster", "owner", "その表、見せて", at=1_700_000_000.0 - 90_000)
        self.assertEqual(store.items("aster"), [])
        raw = (pathlib.Path(self.tmp.name) / "aster.json").read_text(encoding="utf-8")
        self.assertIn("expired_at", raw)

    def test_other_principal_is_separate(self):
        self.store.note_request("aster", "owner", "その表、見せて", at=NOW)
        self.store.note_request("aster", "guest-707", "見せて", at=NOW)
        self.assertEqual(len(self.store.items("aster", principal="owner")), 1)
        self.assertEqual(self.store.clear_delivered("aster", principal="guest-707"), 1)
        self.assertEqual(len(self.store.items("aster", principal="owner")), 1)


class TestOwnerLinesAcrossThreads(unittest.TestCase):
    def test_character_to_character_thread_does_not_hide_owner(self):
        store = ThreadStore()
        store._threads = {
            "a": SimpleNamespace(key=SimpleNamespace(entity="aster"),
                                 transcript=[_turn("user", "owner", "観測記録送ってくれる？", 10.0),
                                             _turn("assistant", "", "……", 11.0)]),
            # 最近活線：aster×ミラ 互刷
            "b": SimpleNamespace(key=SimpleNamespace(entity="aster"),
                                 transcript=[_turn("user", "mira", "……う", 99.0),
                                             _turn("assistant", "", "……う", 100.0)]),
            # 別的角色不該混進來
            "c": SimpleNamespace(key=SimpleNamespace(entity="nori"),
                                 transcript=[_turn("user", "owner", "ノリのはなし", 200.0)]),
        }
        rows = store.recent_owner_lines("aster", 3, owners=("owner",))
        self.assertEqual([row["text"] for row in rows], ["観測記録送ってくれる？"])
        self.assertEqual(store.recent_owner_lines("aster", 3, owners=("nobody",)), [])


class TestHeartbeatSeesBoth(unittest.TestCase):
    def _compose(self, tmp, owner_lines, deliveries):
        prompts = []

        class FakeBrain:
            def run_turn(self, request):
                prompts.append(request.text)
                yield "done", {"text": "うん。"}

        composer = make_heartbeat_composer(
            brain=FakeBrain(), souls=None, persona_cores={}, activity_log=None, affect=None,
            prethinker=None, proactive=SimpleNamespace(recent_texts=lambda entity, limit=3: []),
            raw_log=None, principals=SimpleNamespace(default=lambda: "owner"), speak_lang="ja",
            latest_window=lambda entity: "",
            recent_dialogue=lambda entity, limit=12: [],
            recent_owner_lines=lambda entity, limit=3: owner_lines,
            deliveries=deliveries)
        composer("aster", {"label": "しずか"})
        return prompts[0]

    def test_prompt_has_owner_words_and_owed_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PendingDeliveryStore(tmp)
            store.note_request("aster", "owner", "観測記録送ってくれる？チェックするから",
                               at=time.time() - 7200)
            body = self._compose(tmp, [{"speaker": "owner", "text": "評価対象と評価基準について教えて"}],
                                 store)
        self.assertIn("主人が最後に言っていたこと", body)
        self.assertIn("評価対象と評価基準", body)
        self.assertIn("まだ渡していないもの", body)
        self.assertIn("観測記録送ってくれる", body)


class TestTurnServiceBooksIt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PendingDeliveryStore(self.tmp.name)
        self.turns = TurnService(
            runtime=None, threads=None, pending_store=None, lives={},
            principals=SimpleNamespace(get=lambda pid: SimpleNamespace(
                owner=str(pid) == "owner")),
            pending_delivery=self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_owner_request_is_booked_guest_is_not(self):
        self.turns._note_delivery_request("aster", "owner", "その表、見せて", at=NOW)
        self.turns._note_delivery_request("aster", "guest-707", "その表、見せて", at=NOW)
        self.assertEqual(len(self.store.items("aster", principal="owner")), 1)
        self.assertEqual(len(self.store.items("aster", principal="guest-707")), 0)

    def test_photo_frame_clears_it(self):
        self.turns._note_delivery_request("aster", "owner", "その表、見せて", at=NOW)
        # 只說話不算交付
        self.turns._clear_delivered("aster", "owner", [{"kind": "part", "payload": {"text": "送ったわ"}}])
        self.assertEqual(len(self.store.items("aster", principal="owner")), 1)
        # 真的丟出檔案才算
        self.turns._clear_delivered("aster", "owner", [{"kind": "photo", "payload": {"id": "m1"}}])
        self.assertEqual(self.store.items("aster", principal="owner"), [])


class TestTurnBlockReachesModel(unittest.TestCase):
    """交付欠帳要真的進到**回話那一輪**的 prompt（`[[todo]]`），不是只躺在檔案裡。"""

    def test_messages_include_todo_block(self):
        from agent.brain.messages import build_messages
        from agent.brain.ports import BrainRequest

        request = BrainRequest(
            thread_id="th_1", entity="aster", principal="owner",
            text="採点はどうなってる?", system="",
            meta={"now": time.time(),
                  "delivery_block": "・「観測記録送ってくれる？チェックするから」（11-06 10:20）"})
        body = "\n".join(str(item.get("content") or "") for item in build_messages(request))
        self.assertIn("[[todo]]", body)
        self.assertIn("観測記録送ってくれる", body)
        self.assertIn("次はそちら", body)

    def test_no_todo_block_without_debt(self):
        from agent.brain.messages import build_messages
        from agent.brain.ports import BrainRequest

        request = BrainRequest(thread_id="th_1", entity="aster", principal="owner",
                               text="おはよう", system="", meta={"now": time.time()})
        body = "\n".join(str(item.get("content") or "") for item in build_messages(request))
        self.assertNotIn("[[todo]]", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
