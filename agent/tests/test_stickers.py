#!/usr/bin/env python3
"""表情包（owner 2026-10-29「雙向」「我之後放進來，先按目錄約定做」）。

判據：
  ① 目錄約定：`app/client/assets/stickers/<entity>/stickers.json`＋圖；沒清單就照檔名；壞 id／`../` 一律不認；
  ② 主人送一張 ⇒ 她讀到的是 `[[stamp_in:意思]]`，outbox 記 `asked_sticker`（歷史畫圖不畫字）；
  ③ 她回一張 ⇒ `sticker` 幀（id／url）、對話紀錄寫 `[[stamp_out:意思]]`、outbox 記 `stickers`；
  ④ 工具只在 App 的聊天畫面出現：Discord／通話中都不給（給了她會以為送出去了）；
  ⑤ `GET /api/stickers` 兩端都能看，沒 token ⇒ 401。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from demo_common import *  # noqa: F401,F403

from agent.brain.domain.auth import AuthContext, Role
from agent.brain.media.stickers import StickerCatalog
from agent.brain.runtime.tools import ToolContext
from agent.brain.runtime.tools.sticker import StickerSendTool, sticker_visible
from agent.server import api as api_module

OWNER = AuthContext(principal="owner", role=Role.OWNER, capabilities=frozenset({"chat"}))
GUEST = AuthContext(principal="guest-x", role=Role.GUEST, capabilities=frozenset({"chat"}))

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: 1×1 透明 PNG（內容不重要，只要副檔名對、檔案在）
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def make_catalog(*, manifest: bool = True) -> StickerCatalog:
    root = pathlib.Path(tempfile.mkdtemp(prefix="ba-stickers-"))
    folder = root / "mira"
    folder.mkdir()
    for name in ("smile.png", "ok.png"):
        (folder / name).write_bytes(PNG)
    if manifest:
        (folder / "stickers.json").write_text(json.dumps({"stickers": [
            {"id": "smile", "file": "smile.png", "label": "にこっ", "meaning": "うれしい"},
            {"id": "ok", "file": "ok.png", "label": "OK", "meaning": "わかった"},
            {"id": "Bad Id", "file": "smile.png"},
            {"id": "escape", "file": "../escape.png"},
            {"id": "missing", "file": "missing.png"},
        ]}, ensure_ascii=False), encoding="utf-8")
    return StickerCatalog(root)


class TestCatalog(unittest.TestCase):
    def test_manifest_entries_and_bad_rows_are_dropped(self):
        catalog = make_catalog()
        self.assertEqual([item.id for item in catalog.list("mira")], ["smile", "ok"])
        smile = catalog.get("mira", "SMILE")
        self.assertEqual(smile.meaning, "うれしい")
        self.assertEqual(smile.to_dict()["url"], "/api/stickers/mira/smile")
        self.assertIsNotNone(catalog.path_of(smile))

    def test_without_manifest_file_names_are_used(self):
        catalog = make_catalog(manifest=False)
        self.assertEqual(sorted(item.id for item in catalog.list("mira")), ["ok", "smile"])
        self.assertEqual(catalog.get("mira", "ok").meaning, "ok")

    def test_shared_pack_backs_every_entity(self):
        catalog = make_catalog()
        shared = catalog.root / "_shared"
        shared.mkdir()
        (shared / "smile.png").write_bytes(PNG)     # 同名：角色自己那包優先
        (shared / "bonk.png").write_bytes(PNG)
        (shared / "anim.webp").write_bytes(b"RIFF")  # Unity 解不開 ⇒ 不列
        self.assertEqual([item.id for item in catalog.list("mira")], ["smile", "ok", "bonk"])
        self.assertEqual(catalog.get("mira", "smile").meaning, "うれしい")
        bonk = catalog.get("aster", "bonk")
        self.assertEqual(bonk.to_dict()["url"], "/api/stickers/aster/bonk")
        self.assertEqual(catalog.path_of(bonk), (shared / "bonk.png").resolve())
        self.assertEqual(catalog.path_of(catalog.get("mira", "smile")).parent.name, "mira")
        self.assertEqual(catalog.list("_shared"), [])

    def test_unknown_or_hostile_entity_is_empty(self):
        catalog = make_catalog()
        self.assertEqual(catalog.list("aster"), [])
        self.assertEqual(catalog.list("../mira"), [])

    def test_repo_pack_follows_the_convention(self):
        """倉庫裡那包（舊殼的 104 張，放在共用包）：每個角色都拿得到、圖都在、都寫了意思。"""
        catalog = StickerCatalog(ROOT / "app" / "client" / "assets" / "stickers")
        for entity in ("mira", "aster", "nori"):
            stickers = catalog.list(entity)
            self.assertEqual(len(stickers), 104, entity)
            for item in stickers:
                self.assertIsNotNone(catalog.path_of(item), (entity, item.id))
                self.assertNotEqual(item.meaning, item.id, "真素材要寫 meaning（她靠它挑）")


class TestToolSurface(unittest.TestCase):
    def test_visible_only_in_app_chat(self):
        self.assertTrue(sticker_visible("phone:owner"))
        self.assertTrue(sticker_visible("phone:guest-x"))
        self.assertTrue(sticker_visible("api:image"))
        for source in ("discord:owner", "discord:vc", "phone:call", "api:audio", "trpg:t1"):
            self.assertFalse(sticker_visible(source), source)

    def test_schema_lists_her_own_pack_with_meanings(self):
        tool = StickerSendTool(make_catalog())
        schema = tool.schema_for(entity="mira", source="phone:owner")
        function = schema["function"]
        self.assertEqual(function["name"], "sticker_send")
        self.assertIn("- smile：うれしい\n- ok：わかった", function["description"])
        self.assertEqual(function["parameters"]["required"], ["id"])
        self.assertIsNone(tool.schema_for(entity="aster", source="phone:owner"))
        self.assertIsNone(tool.schema_for(entity="mira", source="discord:owner"))

    def test_registry_asks_schema_for(self):
        state = make_state()
        try:
            tool = state.app.tools.get("sticker.send")
            self.assertIsNotNone(tool)
            tool.catalog = make_catalog()
            names = lambda source: [str((row.get("function") or {}).get("name"))  # noqa: E731
                                    for row in state.app.tools.schemas(
                                        mode="work", entity="mira", source=source)]
            self.assertIn("sticker_send", names("phone:owner"))
            self.assertNotIn("sticker_send", names("phone:call"))
            self.assertNotIn("sticker_send", names("discord:owner"))
        finally:
            state.shutdown()


class StickerTurnCase(unittest.TestCase):
    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        catalog = make_catalog()
        self.state.app.stickers = catalog
        self.state.app.tools.get("sticker.send").catalog = catalog


class TestOwnerSendsSticker(StickerTurnCase):
    def test_brain_reads_the_meaning(self):
        # ⚠️ 2026-10-29：提示語必須**標明是對方發的**（原本 `（スタンプ：…）` 沒主體，
        # 模型會以為那是自己上一輪說的話 ⇒ 她就不會回貼圖）。
        status, data = api_module.prepare_send(
            self.state, {"kind": "sticker", "sticker": "smile", "entity": "mira"}, auth=OWNER)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["text"], "[[stamp_in:うれしい]]")
        self.assertEqual(data["sticker"], "smile")
        self.assertEqual(data["kind"], "text")

    def test_sticker_with_a_line_keeps_the_line(self):
        _, data = api_module.prepare_send(
            self.state, {"kind": "sticker", "sticker": "ok", "text": "了解", "entity": "mira"},
            auth=GUEST)
        self.assertEqual(data["text"], "[[stamp_in:わかった]] 了解")

    def test_unknown_sticker_is_400(self):
        status, data = api_module.prepare_send(
            self.state, {"kind": "sticker", "sticker": "nope"}, auth=OWNER)
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "unknown sticker")

    def test_outbox_remembers_which_sticker(self):
        _, data = api_module.prepare_send(
            self.state, {"kind": "sticker", "sticker": "smile", "entity": "mira"}, auth=OWNER)
        list(self.state.send_stream(**data))
        row = self.state.app.outbox.since("", principal="owner", limit=5)[-1]
        self.assertEqual(row["asked_sticker"], "smile")
        # outbox 的 `asked` 是給客戶端顯示的 ⇒ 存人話版，不留內部標籤
        self.assertEqual(row["asked"], "（スタンプ：うれしい）")
        self.assertNotIn("stamp_in", row["asked"])


class TestSheSendsSticker(StickerTurnCase):
    def setUp(self):
        super().setUp()

        class StickerBrain:
            def run_turn(self, request):
                yield ("delta", {"text": "やったね、オーナー。"})
                yield ("tool_call", {"id": "call_1", "name": "sticker_send",
                                     "arguments": {"id": "smile"}})
                yield ("done", {"text": "やったね、オーナー。", "entity": "mira",
                                "principal": "owner", "model": "test"})

        self.state.app.runtime.ports.brain = StickerBrain()

    def test_frame_transcript_and_outbox(self):
        frames = [payload for kind, payload in self.state.send_stream(
            text="テスト受かった！", entity="mira", principal="owner", mode="work")
            if kind == "frame"]
        stickers = [frame["payload"] for frame in frames if frame.get("kind") == "sticker"]
        self.assertEqual(len(stickers), 1, frames)
        self.assertEqual(stickers[0]["id"], "smile")
        self.assertEqual(stickers[0]["url"], "/api/stickers/mira/smile")

        thread = next(iter(self.state.app.threads.threads()))
        said = [row.text for row in thread.transcript if row.role == "assistant"]
        # 她自己回的那張記成標籤（不寫「（スタンプ：…）」：那是不准她寫進正文的格式）
        self.assertEqual(said[-1], "やったね、オーナー。 [[stamp_out:うれしい]]")

        row = self.state.app.outbox.since("", principal="owner", limit=5)[-1]
        self.assertEqual(row["stickers"], [{"id": "smile", "entity": "mira",
                                            "url": "/api/stickers/mira/smile"}])
        self.assertEqual(row["text"], "やったね、オーナー。")

    def test_unknown_id_is_refused(self):
        tool = StickerSendTool(self.state.app.stickers)
        ctx = ToolContext(entity="mira", principal="owner", thread_id="t",
                          source="phone:owner", voice=None)
        result = tool.run(ctx, {"id": "nope"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "unknown_sticker")
        self.assertEqual(result.frames, [])

    def test_discord_turn_cannot_send(self):
        result = self.state.send("テスト受かった！", entity="mira", principal="owner",
                                 mode="work", source_override="discord:owner")
        kinds = [frame.get("kind") for frame in result["frames"]]
        self.assertNotIn("sticker", kinds)


class TestStickerApi(StickerTurnCase):
    def test_both_ends_can_list(self):
        for auth in (OWNER, GUEST):
            status, data = api_module.list_stickers(self.state, {"entity": ["mira"]}, auth=auth)
            self.assertEqual(status, 200)
            self.assertEqual([row["id"] for row in data["stickers"]], ["smile", "ok"])

    def test_no_token_is_401(self):
        status, _ = api_module.list_stickers(self.state, {}, auth=None)
        self.assertEqual(status, 401)
        status, path = api_module.sticker_file(self.state, "mira", "smile", auth=None)
        self.assertEqual((status, path), (401, None))

    def test_file_lookup(self):
        status, path = api_module.sticker_file(self.state, "mira", "smile", auth=GUEST)
        self.assertEqual(status, 200)
        self.assertEqual(path.read_bytes(), PNG)
        self.assertEqual(api_module.sticker_file(self.state, "mira", "nope", auth=GUEST),
                         (404, None))


class TestStickerHttp(StickerTurnCase):
    def setUp(self):
        super().setUp()
        from agent.server import http as app_server
        app_server.set_state(self.state)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), app_server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def get(self, path, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        request = urllib.request.Request(self.base + path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers.get("Content-Type"), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type"), exc.read()

    def test_list_and_image(self):
        token = self.state.app.auth.owner_token
        status, _, body = self.get("/api/stickers?entity=mira", token)
        self.assertEqual(status, 200)
        url = json.loads(body)["stickers"][0]["url"]
        status, mime, body = self.get(url, token)
        self.assertEqual((status, mime, body), (200, "image/png", PNG))
        self.assertEqual(self.get(url)[0], 401)
        self.assertEqual(self.get("/api/stickers/mira/nope", token)[0], 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
