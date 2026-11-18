"""App 测试的导入路径：仓库根进 sys.path（`app.*`），不碰 Agent。"""

from __future__ import annotations

import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
os.environ.setdefault("BLUEAGENT_BRAIN", "mock")
