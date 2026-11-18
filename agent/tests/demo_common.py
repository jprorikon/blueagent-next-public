#!/usr/bin/env python3
"""Demo 验收的共享工具（纯 stdlib、离线、不碰旧 agent）。

覆盖：
  N1 一对一：正常回、生成线程、写记忆
  N2 群聊旁听：不跑脑、不出声、但记 speaker/addressed_to
  N3 群聊点名：正常回、回给正确的人
  N4 记忆隐私：A 的私池不出现在 B 的召回里
  N5 自动切片：到轮数上限自动封片，摘要非空
  N6 HTTP：静态页、/api/state、/api/send、/api/seal

跑法：
    cd blueagent-next && python3 agent/tests/run_all.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent.parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# 生產不再有預設大腦（不設就啟動失敗）；測試一律明寫 mock
os.environ.setdefault("BLUEAGENT_BRAIN", "mock")

from agent.brain.compose.bootstrap import build as _build_app         # noqa: E402
import demo_people                                                     # noqa: E402
from agent.brain.domain.activity import Activity, StepStatus        # noqa: E402
from agent.brain.compose.state import AppState                      # noqa: E402
from agent.brain.domain.principal import Principal                # noqa: E402
from agent.brain.domain.thread import MemoryScope                 # noqa: E402
from agent.brain.runtime.activity_store import ActivityStore       # noqa: E402
from agent.brain.runtime.life_loop import LifeLoop                 # noqa: E402


def build(*args, seed: bool = True, **kwargs):
    """`bootstrap.build` ＋ 測試小世界（Alice／Bob、關係圈、立場、種子記憶）。

    生產的 `build()` 只有主人；這些假資料在 `tests/demo_people.py`（2026-10-25 搬出生產）。
    """
    demo = _build_app(*args, **kwargs)
    demo_people.install(demo, seed=seed)
    return demo


# 每日報是「21 點檔」的活動：測試若剛好在那個時段跑，活動輪換會挑到它、把別人的斷言擠掉。
# 這裡統一把它關掉；要驗每日報的測試自己開（tests/test_daily_report.py）。
os.environ.setdefault("BLUEAGENT_REPORT_HOUR", "off")
# 生活步進一律走離線替身（owner 2026-11-03）：以前 demo 會真的撥模型端點，端點一慢
# 全量就從 2.5 分鐘變 30 分鐘，「等 2 秒 cursor 要動」那幾條也跟著飄。要驗真呼叫的測試
# 自己在用例裡把這個變數設成 0（或直接注入假的 step_call）。
os.environ.setdefault("BLUEAGENT_STEP_CALL_OFFLINE", "1")
# 線的回收器按牆鐘關線；測試的線用假時間戳（at=1000…），開機那一次會把它們全收掉 ⇒ 測試預設關，
# 要驗回收的用例直接呼叫 `ThreadReaper.run_once(now=…)`（tests/test_thread_reaper.py）。
os.environ.setdefault("BLUEAGENT_THREAD_REAP", "0")


def make_state(attention=None) -> AppState:
    demo = build(brain_delay=0.0, data_dir=tempfile.mkdtemp(prefix="ba-next-"),
                 step_duration=0.02, life_pause=0.005,
                 attention_config=attention)
    demo.principals.register(Principal("owner", "主人", owner=True), replace=True)
    return AppState(demo)


def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def owner_headers(state) -> dict:
    return auth_headers(state.app.auth.owner_token)


def done_text(result: dict) -> str:
    for frame in result.get("frames") or []:
        if frame.get("kind") == "done":
            return str((frame.get("payload") or {}).get("text") or "")
    return ""
