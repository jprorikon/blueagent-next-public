#!/usr/bin/env python3
"""`user.md` v2：**identity（手寫）＋ recent（自動，4k token 預算）**（owner 2026-10-22）。

判據：
  ① 骨架三段：`权限` / `identity` / `recent`；
  ② `set_call_name` 只改 identity 的第一行，其餘手寫內容原樣保留；
  ③ `remember` 去重、往 recent 追加；
  ④ 超過 4k token ⇒ 把 recent 最舊的幾條**落地成記憶**（callback）並從檔案移除；
  ⑤ 舊格式（`## 偏好` / `## 備註`）自動升級成新結構（不丟內容）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from demo_common import *  # noqa: F401,F403  （先把 repo 放進 sys.path）

from agent.brain.persona.userfile import UserFile, estimate_tokens


class TestStructure(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ba-user-"))
        self.file = UserFile(self.root, host="owner")

    def test_skeleton_has_three_sections(self):
        self.file.ensure("owner", name="主人", call_name="オーナー")
        text = self.file.read("owner")
        for title in ("## 権限", "## identity", "## recent"):
            self.assertIn(title, text)
        self.assertTrue(self.file.allows("owner"), "主人那頁預設允許委任")
        self.assertEqual(self.file.call_name("owner"), "オーナー")

    def test_guest_skeleton_is_fail_closed(self):
        self.file.ensure("guest-x", name="客人")
        self.assertFalse(self.file.allows("guest-x"))

    def test_set_call_name_touches_only_the_first_identity_line(self):
        self.file.ensure("owner", name="主人", call_name="オーナー")
        self.file.set_identity("owner", "オーナー\n怕冷，冷氣 26 度")
        self.file.set_call_name("owner", "船長")
        identity = self.file.identity("owner")
        self.assertTrue(identity.startswith("船長"), identity)
        self.assertIn("怕冷", identity, "手寫的其餘內容不能被覆蓋")

    def test_remember_dedupes(self):
        self.file.ensure("owner", name="主人", call_name="オーナー")
        self.assertTrue(self.file.remember("owner", "喜歡焙茶"))
        self.assertFalse(self.file.remember("owner", "喜歡 焙茶"), "只差空白＝同一條")
        self.assertEqual(self.file.recent("owner"), ["喜歡焙茶"])


class TestBudget(unittest.TestCase):
    def test_estimate_tokens_is_monotonic(self):
        self.assertLess(estimate_tokens("短"), estimate_tokens("長" * 50))
        self.assertGreater(estimate_tokens("あ" * 100), 90, "CJK 一字約 1 token")

    def test_overflow_lands_into_memory_and_shrinks_the_file(self):
        landed: list[tuple[str, list[str]]] = []
        file = UserFile(Path(tempfile.mkdtemp(prefix="ba-user-")), host="owner",
                        token_budget=200,
                        on_overflow=lambda who, items: landed.append((who, list(items))))
        file.ensure("owner", name="主人", call_name="オーナー")
        for index in range(30):
            file.remember("owner", f"第 {index} 條很長很長很長很長很長很長很長很長很長很長的回憶內容")
        text = file.read("owner")
        self.assertLessEqual(estimate_tokens(text), 200 + 60, "整理後要回到預算內（容許一批的誤差）")
        self.assertTrue(landed, "超預算一定要把最舊的落地，不是直接丟掉")
        landed_items = [item for _who, items in landed for item in items]
        self.assertTrue(any("第 0 條" in item for item in landed_items), landed_items[:3])
        self.assertNotIn("第 0 條", file.read("owner"), "落地完就要從 recent 移除")

    def test_identity_is_never_compacted(self):
        file = UserFile(Path(tempfile.mkdtemp(prefix="ba-user-")), host="owner",
                        token_budget=120, on_overflow=lambda who, items: None)
        file.ensure("owner", name="主人", call_name="オーナー")
        file.set_identity("owner", "オーナー\n絕對不要提他的體重。")
        for index in range(20):
            file.remember("owner", f"閒聊 {index} " + "啊" * 30)
        self.assertIn("絕對不要提他的體重", file.identity("owner"))


class TestJapaneseSkeleton(unittest.TestCase):
    """review 2026-10-28：這一頁每輪都進 prompt，骨架以前是簡體中文（权限／允许／还没有）。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ba-user-ja-"))
        self.file = UserFile(self.root, host="owner")

    def test_new_page_block_is_japanese(self):
        from prompt_lang import chinese_hits
        self.file.ensure("owner", name="主人", call_name="オーナー")
        self.file.ensure("guest-x", name="Alice")
        for who in ("owner", "guest-x"):
            block = self.file.block(who)
            self.assertEqual(chinese_hits(block), [], block)
            self.assertIn("## 権限", block)
            self.assertIn("（まだない）", block)
        self.assertIn("委任：許可", self.file.block("owner"))
        self.assertIn("委任：不可", self.file.block("guest-x"))

    def test_clearing_recent_writes_the_japanese_placeholder(self):
        self.file.ensure("owner", name="主人")
        self.file.remember("owner", "焙茶が好き")
        self.file._write_recent(self.file.path_for("owner"), "owner", [])
        self.assertIn("（まだない）", self.file.read("owner"))
        self.assertNotIn("还没有", self.file.read("owner"))

    def test_legacy_page_is_upgraded_and_still_allows(self):
        """舊檔（`## 权限`／`委任：允许`／說明行／`（还没有）`）：骨架升級成日語，值照認。"""
        from prompt_lang import chinese_hits
        path = self.root / "owner.md"
        path.write_text(
            "# 主人（owner）\n\n## 权限\n委任：允许\n"
            "（这一行是**唯一**的委任开关；代码判它＋token 角色，两者都通过才放行。）\n\n"
            "## identity\nオーナー\n\n## recent\n（还没有）\n", encoding="utf-8")
        self.assertTrue(self.file.allows("owner"), "升級前：舊值照認")
        block = self.file.block("owner")
        self.assertEqual(chinese_hits(block), [], block)
        self.assertIn("委任：許可", block, "值在 prompt 裡重畫成日語")
        text = self.file.read("owner")
        self.assertIn("## 権限", text)
        self.assertNotIn("## 权限", text)
        self.assertIn("委任：允许", text, "開關的值本身不替他改")
        self.assertTrue(self.file.allows("owner"), "升級後仍放行")

    def test_legacy_deny_stays_denied(self):
        path = self.root / "guest-x.md"
        path.write_text("# x（guest-x）\n\n## 权限\n委任：拒绝\n\n## identity\nx\n\n## recent\n",
                        encoding="utf-8")
        self.assertFalse(self.file.allows("guest-x"))
        self.assertIn("委任：不可", self.file.block("guest-x"))


class TestMigration(unittest.TestCase):
    def test_old_format_is_upgraded_without_losing_content(self):
        root = Path(tempfile.mkdtemp(prefix="ba-user-old-"))
        path = root / "owner.md"
        path.write_text(
            "# 主人（owner）\n\n## 权限\n委任：允许\n\n## 称呼\nオーナー\n\n"
            "## 偏好（自动累积，最多 12 条）\n- 喜歡焙茶\n- 怕冷\n\n## 备注\n討厭被叫老師\n",
            encoding="utf-8")
        file = UserFile(root, host="owner")
        file.ensure("owner", name="主人")
        text = file.read("owner")
        self.assertIn("## identity", text)
        self.assertIn("## recent", text)
        self.assertIn("オーナー", file.identity("owner"))
        self.assertIn("討厭被叫老師", file.identity("owner"))
        self.assertEqual(file.recent("owner"), ["喜歡焙茶", "怕冷"])
        self.assertTrue(file.allows("owner"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
