#!/usr/bin/env python3
"""跑全部離線驗收（Agent ＋ App）：python3 agent/tests/run_all.py

只跑 App 那一份：python3 app/tests/run_all.py
"""

from __future__ import annotations

import os
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))
# 生產沒有預設大腦（不設就啟動失敗）；測試明寫 mock
os.environ.setdefault("BLUEAGENT_BRAIN", "mock")
# 線的回收器按牆鐘關線；測試的線用假時間戳（at=1000…），開機那一次會把它們全收掉 ⇒ 測試預設關，
# 要驗回收的用例直接呼叫 `ThreadReaper.run_once(now=…)`（tests/test_thread_reaper.py）。
os.environ.setdefault("BLUEAGENT_THREAD_REAP", "0")

if __name__ == "__main__":
    # 兩個 discover 各用一個 loader：同一個 loader 第二次 discover 會沿用它自己釘住的
    # `_top_level_dir`（第一次是 agent/tests），於是 app/tests 被判「not importable」
    # （2026-11-09 實測：`Start directory is not importable: .../app/tests`）。
    agent_suite = unittest.TestLoader().discover(str(HERE), pattern="test_*.py")
    app_suite = unittest.TestLoader().discover(str(REPO / "app" / "tests"), pattern="test_*.py")
    suite = unittest.TestSuite([agent_suite, app_suite])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
