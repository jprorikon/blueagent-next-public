#!/usr/bin/env python3
"""只跑 App（App Server＋Unity 文案／设计）的離線驗收：python3 app/tests/run_all.py

全部（Agent ＋ App）：python3 agent/tests/run_all.py
"""

from __future__ import annotations

import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import app_paths  # noqa: E402,F401

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(HERE), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
