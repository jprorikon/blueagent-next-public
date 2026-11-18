"""E3 第一步（架構審查 2026-11-15，2026-11-17）：組合根 `App` 上被設定的每一個欄位都要先宣告。

以前整合層啟動時臨時 `setattr` 了五個（identity／trpg／trpg_seats／alerts／discord_by_entity），
讀的人只能 `getattr(app, "x", None)` 猜。現在都在 `compose/app_root.py` 宣告；這裡掃生產碼的
`app.<名字> = …`／`state.app.<名字> = …`，有沒宣告的就紅。
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.app_root import App
from agent.brain.compose.bootstrap import App as ReExported

ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSIGN = re.compile(r"\b(?:state\.app|self\.app|app)\.([a-z_][a-z0-9_]*)\s*=(?!=)")


class TestAppRootFields(unittest.TestCase):
    def test_bootstrap_still_exports_the_same_class(self):
        self.assertIs(ReExported, App)

    def test_every_assigned_attribute_is_declared(self):
        declared = {item.name for item in dataclasses.fields(App)}
        declared |= {name for name, value in vars(App).items() if isinstance(value, property)}
        missing = []
        for path in ROOT.rglob("*.py"):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for name in ASSIGN.findall(line):
                    if name not in declared:
                        missing.append(f"{path.relative_to(ROOT)}:{number} app.{name}")
        self.assertEqual(missing, [], "在 compose/app_root.py 的 App 宣告這些欄位")

    def test_the_five_former_setattrs_are_declared_with_safe_defaults(self):
        app = App(**{item.name: None for item in dataclasses.fields(App)
                     if item.default is dataclasses.MISSING and item.default_factory is dataclasses.MISSING})
        self.assertIsNone(app.identity)
        self.assertIsNone(app.trpg)
        self.assertIsNone(app.trpg_seats)
        self.assertIsNone(app.alerts)
        self.assertEqual(app.discord_by_entity, {})



class TestIdentityMemorySlice(unittest.TestCase):
    """E3 第二片（2026-11-17）：身分／記憶邊界的標註指得到真的類別，組好的 App 實際就是這些型別。"""

    EXPECTED = {
        "entities": "EntityRegistry",
        "principals": "PrincipalRegistry",
        "memory": "ProbeMemoryPort",
        "accounts": "AccountStore | None",
        "identity": "IdentityMap | None",
    }

    def namespace(self):
        from agent.brain.compose.identity import IdentityMap
        from agent.brain.domain.entity import EntityRegistry
        from agent.brain.domain.principal import PrincipalRegistry
        from agent.brain.ports import ProbeMemoryPort
        from agent.brain.runtime.identity.accounts import AccountStore
        return {"IdentityMap": IdentityMap, "EntityRegistry": EntityRegistry,
                "PrincipalRegistry": PrincipalRegistry, "ProbeMemoryPort": ProbeMemoryPort,
                "AccountStore": AccountStore}

    def test_annotations_name_real_classes(self):
        fields = {item.name: item.type for item in dataclasses.fields(App)}
        names = self.namespace()
        for name, expected in self.EXPECTED.items():
            with self.subTest(field=name):
                self.assertEqual(fields[name], expected)
                head = expected.split(" | ")[0]
                self.assertIn(head, names, "TYPE_CHECKING 底下要 import 它")
                self.assertTrue(isinstance(names[head], type))

    def test_built_app_matches_the_annotations(self):
        import tempfile
        names = self.namespace()
        app = build(data_dir=tempfile.mkdtemp(prefix="ba-e3-"), brain_delay=0.0)
        self.addCleanup(app.shutdown_lives)
        self.assertIsInstance(app.entities, names["EntityRegistry"])
        self.assertIsInstance(app.principals, names["PrincipalRegistry"])
        self.assertIsInstance(app.accounts, names["AccountStore"])
        self.assertTrue(app.identity is None or isinstance(app.identity, names["IdentityMap"]))
        for method in ("recall", "recall_detail", "record", "probe"):
            with self.subTest(memory_method=method):
                self.assertTrue(callable(getattr(app.memory, method, None)),
                                f"{type(app.memory).__name__} 不滿足 ProbeMemoryPort.{method}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
