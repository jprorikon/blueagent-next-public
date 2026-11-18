#!/usr/bin/env python3
"""**沿一條線掃色塊**（M3 的像素判據之二）：在同一組**設計座標**上掃真相／Unity 兩張圖，
把連續同色的區段印出來——「那條 1px 的線在不在、在哪、寬多少」用這一支問。

為什麼要有它（2026-10-11 的實例）：`ab_compare.py` 只比文字、`rect_audit.py` 只有 Unity dump
（真相側沒有容器座標）、`color_probe.py` 只有「某色的外接框」。而這一輪要驗的東西全是
「框線／分隔帶／漸層交界」——**單點取樣會騙人**（1px 的線在 1.565 倍的輸出裡可能剛好被跨過），
沿線掃才看得到「有幾 px、在哪一段」。先前每次都在 here-doc 裡重寫一遍，收成工具。

用法（**用 `.venv-yolo` 的 python**，本機沒有 PIL）：
    .venv-yolo/bin/python tools/slice_probe.py column 300 --from 310 --to 365 \
        Builds/webui-settings-service.png Builds/unity-settings-service.png
    .venv-yolo/bin/python tools/slice_probe.py row 60 --from 10 --to 450 <真相> <Unity>

* 座標一律**設計格**（460 寬；Unity 的圖也是 460 寬，不必換算）。
* 兩張圖的順序＝(真相, Unity)；標籤自動取檔名。
* 連續同色合併成一段（>=1px），印 `y a..b (n) rgb`。
"""
import argparse
import os
import sys

from PIL import Image

DESIGN_WIDTH = 460.0


def runs(image, scale, column=None, row=None, lo=0, hi=0):
    """沿著 column（掃 y）或 row（掃 x）印出同色區段。"""
    out = []
    previous = None
    start = None
    for step in range(lo, hi):
        if column is not None:
            x, y = int(round(column * scale)), int(round(step * scale))
        else:
            x, y = int(round(step * scale)), int(round(row * scale))
        x = min(max(x, 0), image.size[0] - 1)
        y = min(max(y, 0), image.size[1] - 1)
        color = image.getpixel((x, y))
        if previous is None:
            previous, start = color, step
        elif color != previous:
            out.append((start, step - 1, previous))
            previous, start = color, step
    if previous is not None:
        out.append((start, hi - 1, previous))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("axis", choices=["column", "row"], help="column＝固定 x 掃 y；row＝固定 y 掃 x")
    ap.add_argument("at", type=float, help="固定的那個座標（設計格）")
    ap.add_argument("--from", dest="lo", type=int, required=True)
    ap.add_argument("--to", dest="hi", type=int, required=True)
    ap.add_argument("images", nargs=2, help="兩張圖：先是真相、後是 Unity")
    args = ap.parse_args()

    for path in args.images:
        image = Image.open(path).convert("RGB")
        scale = image.size[0] / DESIGN_WIDTH
        label = os.path.basename(path)
        where = ("x" if args.axis == "column" else "y") + f"={args.at:g}"
        print(f"── {label}（{image.size[0]}x{image.size[1]}，{args.axis} {where}，"
              f"{args.lo}..{args.hi}）")
        for start, end, color in runs(image, scale,
                                      column=args.at if args.axis == "column" else None,
                                      row=args.at if args.axis == "row" else None,
                                      lo=args.lo, hi=args.hi):
            print(f"   {'y' if args.axis == 'column' else 'x'} {start:4d}..{end:4d} "
                  f"({end - start + 1:3d}px) rgb{color}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
