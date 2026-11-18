#!/usr/bin/env python3
"""S0：App Server 社交核心无 Agent 依赖，旧组合入口行为保持。"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

import app_paths  # noqa: F401 - 直接运行时也建立仓库导入路径

from app.server.social.models import Actor, Post
from app.server.social.service import BoardService


ROOT = pathlib.Path(__file__).resolve().parents[2]


class AppServerBoundaryCase(unittest.TestCase):
    def test_social_core_never_imports_agent(self):
        offenders = []
        for path in (ROOT / "app" / "server").rglob("*.py"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if ("blueagent_next" in line or "brain." in line or "from brain" in line or "agent." in line
                        or "adapter." in line or "from adapter" in line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(offenders, [])

    def test_standalone_service_has_no_implicit_agent_projection(self):
        with tempfile.TemporaryDirectory(prefix="ba-app-server-") as folder:
            service = BoardService(folder, clock=lambda: 100.0)
            self.addCleanup(service.close)
            owner = Actor("owner", "owner", "主人")
            self.assertEqual(service.feed(owner)["posts"], [])
            created = service.post(owner, "平台自己的投稿")["post"]
            self.assertEqual(service.feed(owner)["posts"][0]["id"], created["id"])
            self.assertEqual(service.feed(owner)["posts"][0]["origin"], "manual")

    def test_legacy_projection_and_local_board_are_retired(self):
        for relative in ("agent/brain/domain/post.py", "agent/tools/export_board_projection.py",
                         "agent/tools/rebuild_board.py", "brain", "server", "adapter", "tools"):
            self.assertFalse((ROOT / relative).exists(), relative)
        self.assertEqual(list((ROOT / "agent/brain/runtime/board").rglob("*.py")), [])
        offenders = []
        for folder in ("brain", "adapter", "server"):
            for path in (ROOT / "agent" / folder).rglob("*.py"):
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if "app.server" in line:
                        offenders.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(offenders, [])

    def test_root_holds_only_app_agent_mr(self):
        """2026-11-09 owner：根目錄只留 app／agent／mr 三個本體（＋跨三者的 docs、deploy 與幾個單檔）。"""
        import subprocess
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, text=True).stdout.split("\0")
        if not tracked:
            self.skipTest("不在 git 工作樹裡")
        top = {path.split("/", 1)[0] for path in tracked if path}
        self.assertEqual(top, {"agent", "app", "mr", "docs", "deploy", "docker-compose.yml",
                               "README.md", "DESIGN.md", ".gitignore", ".dockerignore",
                               ".env.example", "THIRD_PARTY_NOTICES.md"})

    def test_agent_and_app_images_have_independent_contents(self):
        dockerfile = (ROOT / "agent" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY agent/brain ./agent/brain", dockerfile)
        self.assertIn("COPY agent/adapter ./agent/adapter", dockerfile)
        self.assertNotIn("COPY blueagent_next", dockerfile)
        self.assertFalse((ROOT / "blueagent_next").exists())
        self.assertNotIn("COPY app/server", dockerfile)
        self.assertNotIn("COPY app ./app", dockerfile)
        self.assertNotIn("COPY assets ./assets", dockerfile)
        self.assertNotIn("COPY app/client/assets/stickers/", dockerfile)
        app_dockerfile = (ROOT / "app" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY app/server ./app/server", app_dockerfile)
        self.assertIn("COPY app/client/assets ./app/client/assets", app_dockerfile)
        self.assertNotIn("COPY app ./app", app_dockerfile)
        self.assertNotIn("COPY agent", app_dockerfile)
        self.assertNotIn("COPY blueagent_next", app_dockerfile)
        self.assertFalse((ROOT / "Dockerfile").exists())
        self.assertFalse((ROOT / "Dockerfile.app").exists())

    def test_compose_keeps_agent_and_platform_databases_separate(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        agent = compose.split("\n  blueagent-next:\n", 1)[1].split("\n  app-server:\n", 1)[0]
        app_server = compose.split("\n  app-server:\n", 1)[1].split("\n  app-gateway:\n", 1)[0]
        self.assertIn("- ./data:/app/data", agent)
        self.assertNotIn("app-data", agent)
        self.assertIn("- ./app-data:/app/data", app_server)
        self.assertNotIn("- ./data:/app/data", app_server)
        self.assertNotIn("BLUEAGENT_APP_PUBLIC", compose)
        self.assertNotIn("/vol1/", agent)
        self.assertNotIn(".ts." + "net", agent)
        self.assertNotIn("network_mode: host", agent)
        self.assertNotIn("env_file:", agent)

    def test_client_assets_have_one_canonical_home(self):
        self.assertTrue((ROOT / "app/client/assets/stickers/_shared/stickers.json").is_file())
        self.assertEqual(len(list((ROOT / "app/client/assets/stickers/_shared").glob("*.png"))), 104)
        self.assertFalse((ROOT / "assets/stickers").exists())
        models = ROOT / "app/client/BlueAgentUnity/Assets/Resources/models3d"
        self.assertFalse(models.exists(), "公开版不分发第三方 3D 模型")
        self.assertEqual(list(ROOT.rglob("*.fbx")), [])
        for name, symbol in (("stickers", "StickerCatalog"), ("motion", "MotionLibrary")):
            legacy = (ROOT / f"agent/brain/media/{name}.py").read_text(encoding="utf-8")
            self.assertIn(f"from app.client.assets.{name} import", legacy)
            self.assertNotIn(f"class {symbol}", legacy)
        stickers = (ROOT / "app/client/BlueAgentUnity/Assets/Scripts/Next/NextStickers.cs").read_text(
            encoding="utf-8")
        self.assertIn('return "/v1/stickers/"', stickers)


if __name__ == "__main__":
    unittest.main(verbosity=2)
