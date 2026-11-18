#!/usr/bin/env python3
"""編譯某個角色的人格 core（SPIN 第 ① 段，P0）。

    python3 agent/tools/compile_persona_core.py mira
    python3 agent/tools/compile_persona_core.py mira --show      # 只印出來、不寫檔
    python3 agent/tools/compile_persona_core.py --all               # 所有角色都編

產出：`data/persona/<entity>.core.json`（帶人格檔 hash；改了人格檔會自動失效重編）。
不進版控（`data/` 本來就在 gitignore）。

跑在容器裡用 `docker exec -w /app -e PYTHONPATH=/app blueagent-next python3 agent/tools/compile_persona_core.py …`。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from agent.brain.persona.core import (compile_core, core_hash, http_call,  # noqa: E402
                                        load_core, prompt_block, save_core)
from agent.brain.persona.soul import SoulLibrary  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("entity", nargs="?", default="mira")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--show", action="store_true", help="只印、不寫檔")
    ap.add_argument("--data", default=os.environ.get("BLUEAGENT_DATA_DIR") or str(ROOT / "data"))
    # 人格檔在 `agent/persona/`（舊預設 `souls/` 不存在 ⇒ 直接 `--all` 會全部跳過，2026-10-24 修）
    ap.add_argument("--souls", default=os.environ.get("BLUEAGENT_SOUL_DIR")
                    or str(ROOT / "agent" / "persona"))
    ap.add_argument("--force", action="store_true", help="已有 core 也重編")
    a = ap.parse_args()

    souls = SoulLibrary(a.souls)
    targets = ["mira", "aster", "nori"] if a.all else [a.entity]
    call = http_call(os.environ)
    if call is None:
        print("沒有可用的 LLM（遠端端點要 BLUEAGENT_LLM_API_KEY／DEEPSEEK_API_KEY）")
        return 2

    for entity in targets:
        soul_text = souls.block(entity)
        if not soul_text.strip():
            print(f"[{entity}] 沒有人格檔（{a.souls}/{entity}.md）⇒ 跳過")
            continue
        existing = load_core(pathlib.Path(a.data) / "persona", entity, soul_text=soul_text)
        if existing and not a.force:
            print(f"[{entity}] 已有 core（hash {core_hash(existing)}、"
                  f"{len(existing.get('disposition_rules') or [])} 條傾向）⇒ 沿用")
            print(prompt_block(existing))
            continue
        # 編譯的素材只放**角色自己的檔**：整份 block 還包含共通 SOUL.md（語言／聲的作法／
        # 邊界那些操作規約），餵進去模型會把「聲的窗口」當成她的人格
        #（2026-10-24 實測：三隻都烤成「規律、簡短、守邊界」的同一個樣板）。
        role_text = souls.character(entity)
        core = compile_core(role_text or soul_text, soul_text=soul_text, call=call, log=print)
        if core is None:
            print(f"[{entity}] 編譯失敗（模型輸出形狀不對或連不上）")
            continue
        if a.show:
            print(json.dumps(core, ensure_ascii=False, indent=1))
            continue
        path = save_core(pathlib.Path(a.data) / "persona", entity, core)
        print(f"[{entity}] 已寫 {path}（hash {core_hash(core)}）")
        print(prompt_block(core))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
