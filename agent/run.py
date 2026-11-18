#!/usr/bin/env python3
"""啟動 Agent：python3 agent/run.py（或在倉庫根 python3 -m agent.run）"""

from __future__ import annotations

import os
import pathlib
import sys

# ⚠️ 2026-10-31：`data/` 是**容器（root）與主機側工具（986）共用**的目錄。
#   容器寫出來一律 0600 root:root ⇒ 主機側的工具（rotate/doctor/backfill）讀不到，還會被
#   `accounts.json` 的「讀不到就隔離」誤判成壞檔（同一天 12 次）。改成 group 可寫（0664／0775）。
#   ⚠️ 這台 NAS 不吃 setgid 繼承，新檔的 group 不會自己變成 980 ⇒ 擁有者要靠
#   `runtime/fileperm.adopt_owner` 寫完就地 chown（只接在幾個主機側工具會讀的檔，見那支的說明）。
os.umask(0o002)

REPO = pathlib.Path(__file__).resolve().parents[1]   # 倉庫根：包名一律 agent.*／app.*
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.server.main import main  # noqa: E402

if __name__ == "__main__":
    main()
