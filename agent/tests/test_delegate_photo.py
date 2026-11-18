#!/usr/bin/env python3
"""委任出圖的交付形狀（owner 2026-11-04「委任出圖需要同時實現 discord 和 app 兩邊」）。

判據：
  ① 圖片產物**存進 media store**，工具回 `photo` 幀（`{id, url, name}`）而不是帶路徑的 `artifact`；
  ② `photo.url` 就是 `/api/media/<id>`（App 直接拿得到；Discord 用 id 反查檔案）；
  ③ 非圖片的產物仍走 `artifact`（帶路徑，只給主人那端看）；
  ④ 存不進媒體倉時退回 `artifact`，不能讓交付整個爛掉。
"""

from __future__ import annotations

import pathlib
import tempfile
import types
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.media.store import MediaStore
from agent.brain.runtime.delegate import DelegateResult
from agent.brain.runtime.tools.delegate import DelegateTool, is_image_task

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)


class _Gateway:
    """假的委任通道：回一份 artifacts（不真的去叫 Hermes）。"""

    base_url = "http://127.0.0.1:18642"
    enabled = True

    def __init__(self, artifacts):
        self._artifacts = artifacts

    def run(self, task: str, **kwargs) -> DelegateResult:
        return DelegateResult(ok=True, text="できた", receipt="【交付の控え】届いた",
                              artifacts=list(self._artifacts), run_id="r1")


class _Ctx:
    principal = "owner"
    entity = "mira"
    thread_id = "th_1"
    source = "phone:owner"


class TestDelegatePhotoDelivery(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.media = MediaStore(self.root / "media")
        self.ctx = _Ctx()

    def _tool(self, artifacts):
        return DelegateTool(_Gateway(artifacts), media=self.media)

    def _file(self, name: str, data: bytes = PNG) -> str:
        path = self.root / name
        path.write_bytes(data)
        return str(path)

    def test_image_artifact_becomes_photo_frame(self):
        tool = self._tool([self._file("draw.png")])
        result = tool.run(self.ctx, {"task": "かわいい絵を描いて {{OUT}}.png"})
        self.assertTrue(result.ok)
        kinds = [frame["kind"] for frame in result.frames]
        self.assertIn("photo", kinds)
        self.assertNotIn("artifact", kinds, "圖不該再以路徑形式外流")
        photo = next(frame for frame in result.frames if frame["kind"] == "photo")["payload"]
        self.assertEqual(photo["url"], "/api/media/" + photo["id"])
        self.assertEqual(photo["name"], "draw.png")
        self.assertIsNotNone(self.media.path_of(photo["id"]), "圖要真的存進媒體倉")

    def test_non_image_task_is_refused_before_hermes(self):
        """委任只剩出图（PENDING Agent.6）：描述写了还不够，代码要挡；不是出图就别送 Hermes。"""
        gateway = _Gateway([self._file("notes.txt", b"hello")])
        tool = DelegateTool(gateway, media=self.media)
        for task in ("メモを書いて", "このファイルを整理して", "帮我查一下天气", "write a python script"):
            result = tool.run(self.ctx, {"task": task})
            self.assertFalse(result.ok, task)
            self.assertEqual(result.error, "delegate_image_only")
            self.assertIn("terminal", result.data["text"])
        for task in ("アスターの立ち絵を描いて", "做一张海报", "an illustration of a cat", "生成 {{OUT}}.webp"):
            self.assertTrue(is_image_task(task), task)

    def test_non_image_stays_artifact(self):
        tool = self._tool([self._file("notes.txt", b"hello")])
        result = tool.run(self.ctx, {"task": "絵を描いて、説明メモも付けて {{OUT}}.png"})
        kinds = [frame["kind"] for frame in result.frames]
        self.assertIn("artifact", kinds)
        self.assertNotIn("photo", kinds)

    def test_missing_file_falls_back_to_artifact(self):
        tool = self._tool([str(self.root / "gone.png")])
        result = tool.run(self.ctx, {"task": "絵"})
        kinds = [frame["kind"] for frame in result.frames]
        self.assertIn("artifact", kinds, "讀不到檔不能讓交付整個消失")

    def test_no_media_store_keeps_old_behaviour(self):
        tool = DelegateTool(_Gateway([self._file("draw.png")]), media=None)
        result = tool.run(self.ctx, {"task": "絵"})
        self.assertEqual([frame["kind"] for frame in result.frames if frame["kind"] != "receipt"],
                         ["artifact"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
