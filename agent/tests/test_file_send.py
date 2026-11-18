#!/usr/bin/env python3
"""`file.send`：她把自己的檔案當附件送出（owner 2026-11-08）。

由來：11-06 主人說「観測記録送ってくれる？チェックするから」，她那一輪回空——她手上只有
委任出圖（只能出圖）與貼圖，沒有「交一份檔案」的手段。

判據：
  A. 只送得出**她自己的**兩個根（notes／works／vm）；路徑穿越與外部絕對路徑一律擋
  B. 進 `FileStore`（`data/files`），回 `file` 幀（`{id, name, bytes}`），Discord 那側才拿得到本體
  C. 空檔／超過上限不送；檔名清洗（不能帶路徑分隔）
  D. `photo`／`artifact`／`file` 三種幀都算「交付了」——未渡しの依頼會被結清
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from agent.brain.media.files import FileStore, safe_name  # noqa: E402
from agent.brain.runtime.tools.file_send import FileSendTool  # noqa: E402


class TestFileStore(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileStore(tmp)
            row = store.save(b"hello", name="note.md", entity="aster", principal="owner")
            self.assertTrue(row["id"].startswith("f"))
            path = store.path_of(row["id"])
            self.assertIsNotNone(path)
            self.assertEqual(path.read_bytes(), b"hello")
            self.assertEqual(store.row_of(row["id"])["name"], "note.md")

    def test_empty_and_too_large(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileStore(tmp, max_bytes=4)
            with self.assertRaises(ValueError):
                store.save(b"", name="a.txt")
            with self.assertRaises(ValueError):
                store.save(b"12345", name="a.txt")

    def test_name_is_sanitised(self):
        self.assertEqual(safe_name("../../etc/passwd"), "_.._etc_passwd")
        self.assertEqual(safe_name(""), "file.txt")


class TestFileSendTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.files = FileStore(self.base / "files")
        self.tool = FileSendTool(self.files, base=self.base)
        self.ctx = SimpleNamespace(entity="aster", principal="owner", source="discord:dm:owner")
        (self.base / "works" / "aster").mkdir(parents=True)
        (self.base / "works" / "aster" / "観測表.md").write_text("日時|見たこと\n", encoding="utf-8")
        (self.base / "notes" / "aster").mkdir(parents=True)
        (self.base / "notes" / "aster" / "2026-11-08.md").write_text("メモ", encoding="utf-8")
        (self.base / "notes" / "nori").mkdir(parents=True)
        (self.base / "notes" / "nori" / "秘密.md").write_text("だめ", encoding="utf-8")
        (self.base / "secret.txt").write_text("root", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sends_own_work_as_file_frame(self):
        result = self.tool.run(self.ctx, {"path": "works/観測表.md"})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.frames[0]["kind"], "file")
        payload = result.frames[0]["payload"]
        self.assertEqual(payload["name"], "観測表.md")
        self.assertEqual(self.files.path_of(payload["id"]).read_text(encoding="utf-8"),
                         "日時|見たこと\n")

    def test_absolute_path_inside_own_root_is_fine(self):
        target = (self.base / "notes" / "aster" / "2026-11-08.md").resolve()
        self.assertTrue(self.tool.run(self.ctx, {"path": str(target)}).ok)

    def test_traversal_and_other_roots_are_blocked(self):
        for bad in ("../../secret.txt", "../nori/秘密.md",
                    str((self.base / "secret.txt").resolve()),
                    str((self.base / "notes" / "nori" / "秘密.md").resolve())):
            result = self.tool.run(self.ctx, {"path": bad})
            self.assertFalse(result.ok, bad)
            self.assertEqual(result.error, "file_not_allowed", bad)

    def test_listing_when_path_omitted(self):
        """沒有檔名時要能列清單——不然她只能瞎猜（實測 11-08：她跑去 VM 裡 find）。"""
        result = self.tool.run(self.ctx, {})
        self.assertTrue(result.ok)
        paths = [item["path"] for item in result.data["items"]]
        self.assertIn("works/観測表.md", paths)
        self.assertIn("notes/2026-11-08.md", paths)
        self.assertFalse(any("nori" in path for path in paths), "別人的檔案不列")

    def test_fuzzy_name_resolves_when_unique(self):
        """她猜「観測表」也要送得出去——猜名字不該再多花一個工具回合。"""
        (self.base / "works" / "aster" / "観測表-2.md").unlink(missing_ok=True)
        result = self.tool.run(self.ctx, {"path": "観測表"})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["name"], "観測表.md")

    def test_ambiguous_name_is_refused(self):
        (self.base / "works" / "aster" / "観測表-2.md").write_text("x", encoding="utf-8")
        self.assertEqual(self.tool.run(self.ctx, {"path": "観測表"}).error, "file_not_allowed")

    def test_unknown_path_is_refused(self):
        self.assertEqual(self.tool.run(self.ctx, {"path": "works/nope.md"}).error,
                         "file_not_allowed")


class TestFileFrameClearsDelivery(unittest.TestCase):
    def test_file_frame_counts_as_delivered(self):
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))
        from agent.brain.compose.turns import TurnService
        from agent.brain.runtime.inner.pending_delivery import PendingDeliveryStore

        with tempfile.TemporaryDirectory() as tmp:
            store = PendingDeliveryStore(tmp)
            turns = TurnService(runtime=None, threads=None, pending_store=None, lives={},
                                principals=SimpleNamespace(get=lambda pid: SimpleNamespace(
                                    owner=str(pid) == "owner")),
                                pending_delivery=store)
            turns._note_delivery_request("aster", "owner", "その表、見せて", at=0.0)
            self.assertEqual(len(store.items("aster", principal="owner")), 1)
            turns._clear_delivered("aster", "owner", [{"kind": "file", "payload": {"id": "f1"}}])
            self.assertEqual(store.items("aster", principal="owner"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
