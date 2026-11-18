#!/usr/bin/env python3
"""把 owner token 輪替掉（舊的立刻失效，新的只印這一次）。

用法：
    python agent/tools/rotate_owner_token.py            # 直接改 data/auth.json
    python agent/tools/rotate_owner_token.py --print     # 只印目前指紋

為什麼需要工具而不是只靠環境變數：token 現在**只存 SHA-256**，
除了「建立」與「輪替」那一次，任何地方都拿不到明文——這是刻意的。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent.brain.compose.bootstrap import build  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="", help="data 目錄（預設 repo/data 或 BLUEAGENT_DATA_DIR）")
    parser.add_argument("--print", dest="show", action="store_true", help="只印指紋")
    args = parser.parse_args()

    demo = build(data_dir=args.data or None, brain_delay=0, step_duration=0.02,
                 life_pause=0.005)
    try:
        auth = demo.auth
        if args.show:
            print(auth.owner_token_fingerprint)
            return 0
        if auth.owner_token:
            # 這次啟動是「新建」或「明文升級」⇒ 先讓主人有機會抄走現用的那支
            print("目前的 owner token（升級/新建，只顯示這次）：", auth.owner_token)
        token = auth.rotate()
        print("已輪替。新的 owner token（只顯示這次，請立刻抄走）：")
        print(token)
        print("舊 token 從現在起一律 401。")
        return 0
    finally:
        demo.shutdown_lives()


if __name__ == "__main__":
    raise SystemExit(main())
