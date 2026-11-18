#!/usr/bin/env python3
"""套件分層：掃全部 import（含函式內延遲 import），逐包比對允許依賴表。

S4-l 起核心碼在 ``brain/``、外部渠道在 ``adapter/``，舊命名空间不得存在。
2026-11-09 起兩者都在 ``agent/`` 底下、包名帶 ``agent.``；這裡先剝掉前綴再算層。
新頂層包沒登記就失敗；刪掉一條依賴時也應同步收緊這張表。
"""

from __future__ import annotations

import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CORE = ROOT / "agent" / "brain"
ADAPTERS = ROOT / "agent" / "adapter"
LEGACY = ROOT / "blueagent_next"

#: 只列「跨頂層包」的允許方向；同包內的 import 不在此表重複。
ALLOWED: dict[str, set[str]] = {
    "adapters": {"domain", "memory", "runtime"},
    "brain": {"config", "domain", "ports"},
    "compose": {"adapters", "brain", "config", "domain", "media", "memory", "persona",
                "pipelines", "ports", "router", "runtime"},
    "config": set(),
    "domain": set(),
    "games": set(),
    "media": {"config"},
    "memory": {"domain"},
    "persona": {"config"},
    "pipelines": {"domain"},
    "ports": {"domain"},
    "router": {"domain", "pipelines", "ports"},
    "runtime": {"config", "domain", "memory", "pipelines", "ports", "router", "sources",
                "vendor"},
    "sources": {"domain"},
    "vendor": set(),
}


def _imports(path: pathlib.Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
            found.extend((node.lineno, f"{node.module}.{alias.name}") for alias in node.names)
        elif isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
    return found


def _sources():
    yield from CORE.rglob("*.py")
    yield from ADAPTERS.rglob("*.py")


def _bare(module: str) -> str:
    """`agent.brain.x` → `brain.x`（分層表以 agent 內的頂層包為準）。"""
    return module[len("agent."):] if module.startswith("agent.") else module


def _source_layer(path: pathlib.Path) -> str:
    rel = path.relative_to(ROOT / "agent")
    if rel.parts[0] == "brain":
        if len(rel.parts) == 2:
            return "ports" if path.stem == "ports" else "brain"
        return rel.parts[1]
    if rel.parts[0] == "adapter":
        return "adapters"
    return ""


def _layer(module: str) -> str:
    parts = _bare(module).split(".")
    if parts[0] == "server":
        return "server"
    if parts[0] == "brain":
        if len(parts) > 1 and parts[1] in ALLOWED:
            return parts[1]
        return "brain"
    if parts[0] == "adapter":
        return "adapters"
    return ""


def _violations(source_layers, forbidden, *, allow=()) -> list[str]:
    hits = []
    for path in _sources():
        rel = path.relative_to(ROOT)
        layer = _source_layer(path)
        if layer not in source_layers:
            continue
        for line, module in _imports(path):
            target = _layer(module)
            if target and target != layer and (target in forbidden
                                               or (allow and target not in allow)):
                hits.append(f"{rel}:{line} -> {module}")
    return sorted(set(hits))


class TestLayering(unittest.TestCase):
    def test_layering_is_not_vacuous(self):
        """搬目錄後前綴對不上，整張表會「零違規」靜默通過——先證明它真的認得出層。"""
        self.assertEqual(_layer("agent.brain.runtime.life"), "runtime")
        self.assertEqual(_layer("agent.brain.compose.state"), "compose")
        self.assertEqual(_layer("agent.adapter.app_server"), "adapters")
        self.assertEqual(_layer("agent.server.http"), "server")
        self.assertEqual(_source_layer(CORE / "runtime" / "life.py"), "runtime")
        self.assertEqual(_source_layer(ADAPTERS / "app_server.py"), "adapters")
        targets = {_layer(module) for path in _sources() for _, module in _imports(path)}
        self.assertTrue({"runtime", "domain", "compose", "adapters"} <= targets, targets)

    def test_every_top_level_package_is_registered(self):
        discovered = {path.name for path in CORE.iterdir()
                      if path.is_dir() and (path / "__init__.py").exists()}
        discovered.update({"brain", "ports", "adapters"})
        self.assertEqual(discovered, set(ALLOWED), "新包要登記允許依賴；刪包也要同步移除")

    def test_physical_migration_is_complete(self):
        self.assertFalse(LEGACY.exists(), "blueagent_next 整包必須刪除，不得保留兼容命名空间")
        self.assertTrue((ADAPTERS / "trpg/seat.py").is_file())
        self.assertTrue((ADAPTERS / "discord_source/source.py").is_file())
        self.assertTrue((ADAPTERS / "app_server.py").is_file())
        self.assertFalse((ADAPTERS / "trpg_seat").exists())
        self.assertFalse((ADAPTERS / "trpg_hub.py").exists())

    def test_unity_client_physical_migration_is_complete(self):
        client = ROOT / "app" / "client"
        self.assertFalse((ROOT / "unity").exists(), "Unity 工程不得继续留在旧顶层目录")
        self.assertTrue((client / "BlueAgentUnity/Assets/Scripts/Next/NextShell.cs").is_file())
        self.assertTrue((client / "shared/BlueAgent.Logic/BlueAgent.Logic.csproj").is_file())
        self.assertTrue((client / "shared/BlueAgent.Logic.Tests/BlueAgent.Logic.Tests.csproj").is_file())
        self.assertTrue((client / "fixtures/reply_split.json").is_file())
        manifest = (client / "BlueAgentUnity/Packages/manifest.json").read_text(encoding="utf-8")
        self.assertIn('"com.blueagent.logic": "file:../../shared/BlueAgent.Logic"', manifest)
        self.assertNotIn('"com.unity.xr.hands"', manifest)
        self.assertNotIn('"com.unity.xr.meta-openxr"', manifest)

        mr = ROOT / "mr"
        self.assertTrue((mr / "BlueAgentUnity/Assets/Scripts/MR/MrShell.cs").is_file())
        self.assertTrue((mr / "BlueAgentUnity/Assets/Editor/MrBuild.cs").is_file())
        self.assertTrue((mr / "tools/sync_workspace.py").is_file())
        self.assertFalse((client / "BlueAgentUnity/Assets/Scripts/MR").exists())
        self.assertFalse((client / "tools/build_mr_quest.sh").exists())
        mr_manifest = (mr / "BlueAgentUnity/Packages/manifest.json").read_text(encoding="utf-8")
        self.assertIn('"com.blueagent.logic": "file:../../../app/client/shared/BlueAgent.Logic"', mr_manifest)
        self.assertIn('"com.unity.xr.meta-openxr"', mr_manifest)
        phone_build = (client / "BlueAgentUnity/Assets/Editor/HeadlessBuild.cs").read_text(encoding="utf-8")
        self.assertNotIn("MrBuild", phone_build)

    def test_shared_toon_shader_includes_resolve_in_both_projects(self):
        """共用著色器的相對 include 在手機工程要找得到、且不能拉 AR Foundation；MR 覆蓋後拿到真實作。"""
        import re
        phone = ROOT / "app/client/BlueAgentUnity/Assets/Shaders"
        quest = ROOT / "mr/BlueAgentUnity/Assets/Shaders"
        toon = (phone / "Stage3DToon.shader").read_text(encoding="utf-8")
        includes = [name for name in re.findall(r'#include\s+"([^"]+)"', toon)
                    if not name.startswith("Packages/")]
        self.assertIn("MrOcclusion.hlsl", includes)
        for name in includes:
            self.assertTrue((phone / name).is_file(), f"手機工程缺 include：{name}")
        stub = (phone / "MrOcclusion.hlsl").read_text(encoding="utf-8")
        self.assertNotIn("com.unity.xr.arfoundation", stub)
        self.assertIn("half MrVisibility(float3 positionWS)", stub)
        real = (quest / "MrOcclusion.hlsl").read_text(encoding="utf-8")
        self.assertIn("com.unity.xr.arfoundation", real)
        self.assertIn("half MrVisibility(float3 positionWS)", real)
        sync = (ROOT / "mr/tools/sync_workspace.py").read_text(encoding="utf-8")
        self.assertIn("dirs_exist_ok=True", sync, "MR Assets 必須覆蓋在共享 Assets 之後")

    def test_dependency_allowlist(self):
        hits = []
        for layer, allowed in ALLOWED.items():
            hits.extend(_violations({layer}, set(), allow=allowed))
        self.assertEqual(sorted(set(hits)), [])

    def test_demo_package_is_gone(self):
        self.assertFalse((CORE / "demo").exists(), "demo/ 應該已經拆掉")
        hits = []
        for folder in ("brain", "adapter", "server", "tests", "tools"):
            for path in (ROOT / "agent" / folder).rglob("*.py"):
                hits += [f"{path.relative_to(ROOT)}:{line}" for line, module in _imports(path)
                         if _bare(module) in {"brain.demo", "blueagent_next.demo"}
                         or _bare(module).startswith(("brain.demo.", "blueagent_next.demo."))]
        self.assertEqual(hits, [])

    def test_lower_layers_never_import_app_or_server(self):
        lower = set(ALLOWED) - {"compose"}
        self.assertEqual(_violations(lower, {"compose", "server"}), [])

    def test_runtime_never_imports_adapters(self):
        self.assertEqual(_violations({"runtime"}, {"adapters"}), [])

    def test_router_does_not_depend_on_runtime(self):
        self.assertEqual(_violations({"router"}, {"runtime"}), [])

    def test_low_level_clients_do_not_depend_on_runtime(self):
        self.assertEqual(_violations({"brain", "persona", "media"}, {"runtime"}), [])

    def test_server_api_uses_app_services(self):
        hits = []
        for path in (ROOT / "agent" / "server" / "api").rglob("*.py"):
            for line, module in _imports(path):
                if (_layer(module) in {"adapters", "runtime"}
                        and _bare(module) != "adapter.app_protocol"
                        and not _bare(module).startswith("adapter.app_protocol.")):
                    hits.append(f"{path.relative_to(ROOT)}:{line} -> {module}")
        self.assertEqual(sorted(set(hits)), [])

    #: runtime 頂層裡「不依賴任何 runtime 模組」的葉子工具：子包可以用（它們不會把子包拉回頂層的職責網）
    #: 葉子工具：不 import 任何 runtime 模組（`test_runtime_leaves_stay_leaves` 鎖住），子包可以直接用。
    #: text_clean（回覆文字清洗，只用 re）：2026-11-17 runtime/voice 收包時加入——compose 也在用，不該被收進某一個子包。
    RUNTIME_LEAVES = {"brain.runtime.fileperm", "brain.runtime.text_clean"}

    def test_runtime_subpackages_do_not_reach_sideways(self):
        # identity／delivery／ops：2026-11-16 從頂層收進來（架構審核 E5）；voice：2026-11-17（NEXT-STEPS WP-D）
        for name in ("memory", "inner", "identity", "delivery", "ops", "voice"):
            folder = CORE / "runtime" / name
            allowed_prefix = f"brain.runtime.{name}"
            hits = []
            for path in folder.rglob("*.py"):
                for line, module in _imports(path):
                    bare = _bare(module)
                    if (bare.startswith("brain.runtime")
                            and not bare.startswith(allowed_prefix)
                            and not any(bare == leaf or bare.startswith(leaf + ".")
                                        for leaf in self.RUNTIME_LEAVES)):
                        hits.append(f"{path.relative_to(ROOT)}:{line} -> {module}")
            self.assertEqual(sorted(set(hits)), [], f"runtime/{name} 不依賴其他 runtime 職責")

    def test_runtime_leaves_stay_leaves(self):
        """白名單裡的葉子工具真的不 import 任何 runtime 模組（否則子包會經由它繞回頂層）。"""
        for leaf in sorted(self.RUNTIME_LEAVES):
            path = ROOT / "agent" / (leaf.replace(".", "/") + ".py")
            hits = [module for _line, module in _imports(path) if _bare(module).startswith("brain.runtime")]
            self.assertEqual(hits, [], leaf)

    def test_runtime_top_level_module_count_ratchet(self):
        count = len(list((CORE / "runtime").glob("*.py")))
        # 2026-11-16：60 → 50（identity／delivery／ops 收走 10 支）；2026-11-17：50 → 46（voice 收走 4 支）
        self.assertLessEqual(count, 46, "runtime 頂層只准再減少；新職責放進內聚子包")


if __name__ == "__main__":
    unittest.main(verbosity=2)
