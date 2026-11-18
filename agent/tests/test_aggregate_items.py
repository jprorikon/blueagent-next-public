#!/usr/bin/env python3
"""延時聚合送出的形狀換算（owner 2026-11-04「用戶的前台文字／圖片／表情包，設計延時聚合發送」）。

App 把連續幾筆攢成一輪，body 是 `{"items": [{kind: text|photo|sticker, …}]}`。
判據：
  ① 文字按順序接行（她要看到他一次講的幾句）；
  ② 照片排成 `media_list`（VisionStage 逐張看）；
  ③ 表情包變成 prompt 認得的 `[[stamp_in:…]]` 標籤；
  ④ `asked_parts` 留著「他自己發的那幾條」，歷史才畫得跟當時一樣；
  ⑤ 照片只認同一個 principal 上傳的（別人／不存在的 id ⇒ 400）；
  ⑥ 什麼都沒有的 items ⇒ 400。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.server.api.items import build_items


class _Sticker:
    def __init__(self, id: str, meaning: str) -> None:
        self.id = id
        self.meaning = meaning


class _Catalog:
    def get(self, entity, sticker_id):
        return {"smile": _Sticker("smile", "えへへ")}.get(sticker_id)


class _Store:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.rows = {}

    def add(self, media_id: str, principal: str, ext: str = ".png") -> None:
        path = self.root / (media_id + ext)
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        self.rows[media_id] = {"id": media_id, "principal": principal, "ext": ext,
                               "bytes": path.stat().st_size}

    def row_of(self, media_id):
        return self.rows.get(media_id)

    def path_of(self, media_id):
        row = self.rows.get(media_id)
        return (self.root / (media_id + row["ext"])) if row else None


class _App:
    def __init__(self, catalog) -> None:
        self.stickers = catalog


class _State:
    def __init__(self, store, catalog) -> None:
        self.app = _App(catalog)
        self.media = store


class TestBuildItems(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.store = _Store(self.root)
        self.state = _State(self.store, _Catalog())

    def _build(self, items, principal="owner"):
        return build_items(self.state, items, entity="mira", principal=principal)

    def test_texts_are_joined_in_order(self):
        status, out = self._build([{"kind": "text", "text": "これ見て"},
                                   {"kind": "text", "text": "昨日のやつ"}])
        self.assertEqual(status, 200)
        self.assertEqual(out["text"], "これ見て\n昨日のやつ")
        self.assertEqual([part["text"] for part in out["asked_parts"]],
                         ["これ見て", "昨日のやつ"])
        self.assertEqual(out["media_list"], [])

    def test_photo_becomes_media_list(self):
        self.store.add("m1", "owner", ".png")
        self.store.add("m2", "owner", ".jpg")
        status, out = self._build([{"kind": "text", "text": "写真２枚"},
                                   {"kind": "photo", "media_id": "m1"},
                                   {"kind": "photo", "media_id": "m2"}])
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in out["media_list"]], ["m1", "m2"])
        self.assertEqual(out["media_list"][1]["mime"], "image/jpeg")
        self.assertTrue(out["media_list"][0]["path"].endswith("m1.png"))

    def test_sticker_becomes_tag(self):
        status, out = self._build([{"kind": "sticker", "sticker": "smile"}])
        self.assertEqual(status, 200)
        self.assertEqual(out["text"], "[[stamp_in:えへへ]]")
        self.assertEqual(out["stickers"], ["smile"])
        self.assertEqual(out["asked_parts"][0]["kind"], "sticker")

    def test_foreign_or_missing_photo_is_rejected(self):
        self.store.add("m9", "someone-else")
        self.assertEqual(self._build([{"kind": "photo", "media_id": "m9"}])[0], 400)
        self.assertEqual(self._build([{"kind": "photo", "media_id": "nope"}])[0], 400)

    def test_unknown_sticker_is_rejected(self):
        self.assertEqual(self._build([{"kind": "sticker", "sticker": "nope"}])[0], 400)

    def test_empty_items_is_rejected(self):
        self.assertEqual(self._build([])[0], 400)
        self.assertEqual(self._build([{"kind": "text", "text": "   "}])[0], 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
