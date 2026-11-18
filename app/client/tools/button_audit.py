#!/usr/bin/env python3
"""**每一屏、每一顆鈕**：把「畫面上做得出名字的節點」與「`AppShell` 有接線的分支」對一次。

為什麼要有這一支（2026-10-14，owner 的標準是「每一屏每一顆鈕都能點」）：
自驅流程（`ui_flow_all.sh`）只走**動線**（開對話 → 排序 → 回清單…）——**沒走到的鈕漏了也不知道**。
實例：`StickerOpen`（打字列那顆笑臉）節點名有、`Activate` 裡**根本沒有分支**，
一路到「點下去看畫面」才發現。這一支把那種洞**靜態**掃出來。

判準（寧可多報，不要漏報）：
* 「可命中的節點」＝各屏 `UiBuilder` 建出來的**具名**物件（`Panel`／`Text`／`Icon`／`Row`／
  `Column`／`Backdrop`／`EdgeLine`／`Radio`／`TechButton`／`RadialFill`／`StripeFill`／`NewRect`）。
* 「有接線」＝`AppShell.cs` 的 `Activate` 裡出現過那個名字（`name == "X"`），或是靠**前綴**接的
  （`name.StartsWith("Cell")`／`"Tile"`／`"Row"`／`"Tab_"`… ⇒ 那一族一律算接過）。
* 其餘的列出來，人再一顆顆看要接什麼。

用法：
    python3 tools/button_audit.py            # 全部
    python3 tools/button_audit.py --kind talk   # 只看某一屏
"""

import argparse
import pathlib
import re

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "BlueAgentUnity" / "Assets" / "Scripts"

# 建節點的入口（第一個字串參數就是節點名）
NODE_CALLS = [
    "Panel", "Text", "Icon", "Row", "Column", "Backdrop", "EdgeLine", "RoundMask", "Overlay",
    "Radio", "TechButton", "RadialFill", "StripeFill", "NewRect", "Switch", "Bar", "Knob",
]
CALL_RE = re.compile(r'UiBuilder\.(' + "|".join(NODE_CALLS) + r')\(\s*"([^"]{1,60})"')


def screen_nodes(path):
    """一個檔裡所有具名節點（照出現序，去重）。"""
    text = path.read_text(encoding="utf-8")
    seen, out = set(), []
    for _, name in CALL_RE.findall(text):
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def wired_names():
    """`AppShell.cs` 的 `Activate` 接線：等值名 ＋ 前綴族。"""
    text = (SCRIPTS / "AppShell.cs").read_text(encoding="utf-8")
    equal = set(re.findall(r'name\s*==\s*"([^"]+)"', text))
    equal |= set(re.findall(r'name\s*!=\s*"([^"]+)"', text))
    prefixes = set(re.findall(r'name\.StartsWith\(\s*"([^"]+)"', text))
    return equal, prefixes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", default="", help="只看某一屏（檔名前綴，例如 talk／menu／game）")
    args = ap.parse_args()

    equal, prefixes = wired_names()
    total_missing = 0
    for path in sorted(SCRIPTS.glob("*Screen.cs")):
        if args.kind and not path.stem.lower().startswith(args.kind.lower()):
            continue
        missing = []
        for name in screen_nodes(path):
            if name in equal:
                continue
            if any(name.startswith(p) for p in prefixes if p):
                continue
            missing.append(name)
        print(f"── {path.stem}：具名節點 {len(screen_nodes(path))} 個，**沒接線** {len(missing)} 個")
        for name in missing:
            print(f"     - {name}")
        total_missing += len(missing)
    print(f"\n合計沒接線：{total_missing} 個（前綴族已算接過：{sorted(p for p in prefixes if p)}）")
    print("⚠️ 不是每個節點都該有行為（純排版／裝飾）——這一支只負責**列出來**，判斷在人。")


if __name__ == "__main__":
    main()
