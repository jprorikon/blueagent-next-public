#!/usr/bin/env python3
"""斷線不丟交付（契約 v2）：`accepted{turn_id}` ＋ `data/outbox/` ＋ `GET /api/turns`。

判據：
  ① 一輪一開始就給 `turn_id`（端末記得住，才能事後補）；
  ② **客戶端半路斷線**（只讀到第一幀就走）⇒ 這一輪的文字仍然被記下來；
  ③ 重連用 `since=<上一個完整收到的 turn_id>` 補回漏掉的那幾輪；
  ④ ACL：只看得到自己的（主人／guest 不互通）；
  ⑤ 有語音的回合能取回合併後的 wav；音檔超預算時**先丟音檔、文字留著**。
"""

from __future__ import annotations

import io
import pathlib
import tempfile
import unittest
import wave

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.delivery.outbox import TurnOutbox, merge_wavs, new_turn_id


def make_wav(seconds: float = 0.2, rate: int = 32000) -> bytes:
    frames = int(seconds * rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


class TestOutboxStore(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())

    def test_record_and_get(self):
        box = TurnOutbox(self.root, keep=10)
        box.record(turn_id="t1", principal="owner", entity="mira", text="おはよう")
        row = box.get("t1", principal="owner")
        self.assertIsNotNone(row)
        self.assertEqual(row["text"], "おはよう")
        self.assertFalse(row["audio"])

    def test_acl_only_own_turns(self):
        box = TurnOutbox(self.root, keep=10)
        box.record(turn_id="t1", principal="owner", entity="mira", text="内緒")
        self.assertIsNone(box.get("t1", principal="guest-a"), "別人不該讀到")
        self.assertEqual(box.since("", principal="guest-a"), [])

    def test_since_returns_later_turns_in_order(self):
        box = TurnOutbox(self.root, keep=10)
        for index in range(4):
            box.record(turn_id=f"t{index}", principal="owner", entity="mira",
                       text=f"第{index}輪")
        rows = box.since("t1", principal="owner")
        self.assertEqual([row["text"] for row in rows], ["第2輪", "第3輪"])

    def test_since_with_unknown_id_returns_tail(self):
        box = TurnOutbox(self.root, keep=10)
        for index in range(3):
            box.record(turn_id=f"t{index}", principal="owner", entity="mira", text="x")
        rows = box.since("nope", principal="owner", limit=2)
        self.assertEqual(len(rows), 2)

    def test_keep_prunes_old_records_and_audio(self):
        box = TurnOutbox(self.root, keep=10)
        for index in range(14):
            box.record(turn_id=f"t{index}", principal="owner", entity="mira",
                       text="x", audio=make_wav())
        rows = box.since("", principal="owner", limit=100)
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[-1]["turn_id"], "t13")
        self.assertEqual(len(list(self.root.glob("*.wav"))), 10, "被剪掉的回合音檔也該刪")

    def test_audio_budget_drops_audio_but_keeps_text(self):
        box = TurnOutbox(self.root, keep=50, audio_bytes=1)   # 預算小到放不下
        box.record(turn_id="t1", principal="owner", entity="mira", text="有聲音",
                   audio=make_wav(0.5))
        row = box.get("t1", principal="owner")
        self.assertIsNotNone(row, "文字不能一起被丟")
        self.assertFalse(row["audio"])
        self.assertEqual(row["text"], "有聲音")

    def test_audio_roundtrip(self):
        box = TurnOutbox(self.root, keep=10)
        box.record(turn_id="t1", principal="owner", entity="mira", text="x",
                   audio=make_wav(0.3))
        data = box.read_audio("t1", principal="owner")
        self.assertGreater(len(data), 0)
        with wave.open(io.BytesIO(data)) as handle:
            self.assertAlmostEqual(handle.getnframes() / handle.getframerate(), 0.3, places=2)
        self.assertEqual(box.read_audio("t1", principal="guest"), b"")

    def test_merge_wavs_concatenates(self):
        merged = merge_wavs([make_wav(0.1), make_wav(0.2)])
        with wave.open(io.BytesIO(merged)) as handle:
            self.assertAlmostEqual(handle.getnframes() / handle.getframerate(), 0.3, places=2)

    def test_turn_ids_are_unique_and_sortable(self):
        ids = [new_turn_id() for _ in range(20)]
        self.assertEqual(len(set(ids)), 20)
        self.assertEqual(ids, sorted(ids))


class TestStreamOutboxWiring(unittest.TestCase):
    def test_accepted_frame_carries_turn_id(self):
        state = make_state()
        self.addCleanup(state.shutdown)
        first = next(iter(state.send_stream(text="おはよう", entity="mira", principal="owner")))
        kind, payload = first
        self.assertEqual(kind, "frame")
        self.assertEqual(payload["kind"], "accepted")
        self.assertTrue(str(payload["payload"]["turn_id"]).startswith("t"))

    def test_client_disconnect_still_leaves_the_reply(self):
        """只讀第一幀就走人（＝斷線），這一輪的回答仍然要在 outbox 裡。"""
        state = make_state()
        self.addCleanup(state.shutdown)
        stream = state.send_stream(text="おはよう、今日は何する？", entity="mira",
                                   principal="owner")
        kind, payload = next(iter(stream))
        turn_id = payload["payload"]["turn_id"]
        stream.close()                                     # 客戶端走人

        import time
        deadline = time.time() + 8
        row = None
        while time.time() < deadline:
            row = state.app.outbox.get(turn_id, principal="owner")
            if row is not None and (row.get("text") or row.get("error")):
                break
            time.sleep(0.05)
        self.assertIsNotNone(row, "斷線後仍要有紀錄")
        self.assertTrue(row.get("text") or row.get("error"),
                        f"回答不能因為斷線就消失：{row}")
        self.assertGreater(row.get("frames", 0), 0)

    def test_since_recovers_missed_turns(self):
        state = make_state()
        self.addCleanup(state.shutdown)
        first_turn = ""
        for text in ("一つ目", "二つ目", "三つ目"):
            for kind, payload in state.send_stream(text=text, entity="mira", principal="owner"):
                if kind == "frame" and payload.get("kind") == "accepted" and not first_turn:
                    first_turn = payload["payload"]["turn_id"]
        rows = state.app.outbox.since(first_turn, principal="owner", limit=10)
        self.assertGreaterEqual(len(rows), 2, f"應該補得到後面幾輪：{rows}")


class TestApiAccessors(unittest.TestCase):
    def test_list_get_and_audio(self):
        from agent.brain.compose.bootstrap import build
        from agent.brain.compose.state import AppState
        import agent.server.api as api

        root = pathlib.Path(tempfile.mkdtemp())
        demo = build(data_dir=str(root))
        state = AppState(demo)
        self.addCleanup(state.shutdown)
        state.app.outbox.record(turn_id="tX", principal="owner", entity="mira",
                                 text="届いた？", audio=make_wav(0.1))

        class Auth:
            principal = "owner"

        status, body = api.list_turns(state, {"since": [""]}, auth=Auth())
        self.assertEqual(status, 200)
        self.assertEqual(body["turns"][0]["turn_id"], "tX")

        status, row = api.get_turn(state, "tX", auth=Auth())
        self.assertEqual(status, 200)
        self.assertEqual(row["text"], "届いた？")

        status, audio = api.get_turn_audio(state, "tX", auth=Auth())
        self.assertEqual(status, 200)
        self.assertGreater(len(audio), 0)

        class Guest:
            principal = "guest-a"

        status, _ = api.get_turn(state, "tX", auth=Guest())
        self.assertEqual(status, 404, "ACL：別人的回合看不到")
        self.assertEqual(api.get_turn_audio(state, "tX", auth=Guest())[0], 404)


class TestTurnsEntityFilter(unittest.TestCase):
    """`/api/turns` 的 `entity` 與 `since` 要能並存：端末換角色後補拿兩者都帶。

    2026-10-27 的 bug：切角色後畫面還停在 mira 的歷史（客戶端沒歸零），
    而且補拿沒帶 entity ⇒ 同一 principal 的**所有角色**的回合都會補進同一頁。
    """

    def _state(self):
        from agent.brain.compose.bootstrap import build
        from agent.brain.compose.state import AppState
        root = pathlib.Path(tempfile.mkdtemp())
        state = AppState(build(data_dir=str(root)))
        self.addCleanup(state.shutdown)
        ob = state.app.outbox
        ob.record(turn_id="m1", principal="owner", entity="mira", text="m1")
        ob.record(turn_id="r1", principal="owner", entity="aster", text="r1")
        ob.record(turn_id="m2", principal="owner", entity="mira", text="m2")
        ob.record(turn_id="r2", principal="owner", entity="aster", text="r2")
        return state

    def _auth(self):
        class Auth:
            principal = "owner"
        return Auth()

    def _ids(self, state, query):
        import agent.server.api as api
        status, body = api.list_turns(state, query, auth=self._auth())
        self.assertEqual(status, 200)
        return [row["turn_id"] for row in body["turns"]]

    def test_since_with_entity_is_scoped_to_that_entity(self):
        state = self._state()
        self.assertEqual(self._ids(state, {"since": ["m1"], "entity": ["mira"]}), ["m2"])

    def test_entity_only_returns_that_entity_history(self):
        state = self._state()
        self.assertEqual(self._ids(state, {"entity": ["aster"]}), ["r1", "r2"])

    def test_negative_control_since_without_entity_crosses_entities(self):
        # 舊行為（不帶 entity）不能被改壞：同一 principal 的回合照樣連著回
        state = self._state()
        self.assertEqual(self._ids(state, {"since": ["m1"]}), ["r1", "m2", "r2"])

    def test_before_paging_still_works(self):
        state = self._state()
        self.assertEqual(self._ids(state, {"entity": ["mira"], "before": ["m2"]}), ["m1"])

    def test_source_prefix_keeps_app_and_discord_apart(self):
        """App 的對話紀錄只認 `phone:`（owner 2026-10-27：app 與 DC 各自獨立）。

        沒有 `source` 的舊列（2026-10-27 之前寫的）在帶 `source` 查詢時不回——寧可少不可錯。
        """
        state = self._state()
        ob = state.app.outbox
        ob.record(turn_id="p1", principal="owner", entity="mira", text="app 的話",
                  asked="app 問", source="phone:owner")
        ob.record(turn_id="d1", principal="owner", entity="mira", text="dc 的話",
                  asked="dc 問", source="discord:owner")
        ob.record(turn_id="old1", principal="owner", entity="mira", text="舊列沒有 source")

        self.assertEqual(self._ids(state, {"entity": ["mira"], "source": ["phone"]}), ["p1"])
        self.assertEqual(self._ids(state, {"entity": ["mira"], "source": ["discord"]}), ["d1"])
        # 負對照：不帶 source ⇒ 全部都看得到（舊行為不能被改壞）
        self.assertEqual(self._ids(state, {"entity": ["mira"]})[-3:], ["p1", "d1", "old1"])

    def test_source_list_covers_image_turns(self):
        """App 的回合有兩種來源標記：`phone:*`（打字／語音）與 `api:image`（圖片）。

        2026-10-27 實測：端末只帶 `source=phone` ⇒ 他發的那張照片那輪整條看不到
        （history 空、她回「看不到圖」）。所以端末帶的是 `phone,api`。
        """
        state = self._state()
        ob = state.app.outbox
        ob.record(turn_id="txt1", principal="owner", entity="nori", text="打字", asked="打字",
                  source="phone:owner")
        ob.record(turn_id="img1", principal="owner", entity="nori", text="写真、ですか。",
                  asked="寫真", source="api:image")
        ob.record(turn_id="dc1", principal="owner", entity="nori", text="dc 的話", asked="dc",
                  source="discord:owner")
        self.assertEqual(
            self._ids(state, {"entity": ["nori"], "source": ["phone,api"]}), ["txt1", "img1"])
        # 單獨給 api 也要能撈到圖片那輪
        self.assertEqual(self._ids(state, {"entity": ["nori"], "source": ["api"]}), ["img1"])
        # discord 不會被 api/phone 撈到
        self.assertEqual(self._ids(state, {"entity": ["nori"], "source": ["discord"]}), ["dc1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
