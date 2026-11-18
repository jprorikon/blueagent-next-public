#!/usr/bin/env python3
"""像素抽查（M3 的**第二路**判據）：畫上去的**顏色／形狀**用像素量測驗收。

為什麼要有這一支：`ab_compare.py` 只比文字詞條——底色、徽章、左緣那條線、有沒有畫到框外，
它**一句話都不會說**（實測：五子棋的黃框與白棋邊畫到盤外，dump 全綠、縮圖看不出來；
選單卡被釘在頂端也是 dump 全綠）。VL 只看縮圖、只會說「好像沒有」。

用法（**用 `.venv-yolo` 的 python**，本機沒有 PIL）：
    .venv-yolo/bin/python tools/color_probe.py \
        --probe '感想底色|255,246,230|30' Builds/webui-trpg-in_table.png Builds/unity-trpg-in_table.png

* `--probe`：`名稱|r,g,b|容差`（可多個）。
* 兩張圖尺寸不同（真相 460×818 ＝ Unity 460×818（2026-10-12 起與真相 1:1））⇒ 比**佔比**（像素數／總像素），
  並印出外接矩形（相對座標 0..1）——位置有沒有跑掉看那個框。
* 判讀：兩邊佔比同一個量級（差 2 倍以內）＝過；一邊 0、一邊幾百 ＝ 那一格沒畫（或畫到框外）。
"""
import argparse
import sys

from PIL import Image


def probe(path, color, tolerance):
    image = Image.open(path).convert("RGB")
    width, height = image.size
    pixels = image.load()
    count = 0
    x0 = y0 = 10 ** 9
    x1 = y1 = -1
    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y]
            if abs(r - color[0]) <= tolerance and abs(g - color[1]) <= tolerance and abs(b - color[2]) <= tolerance:
                count += 1
                x0 = min(x0, x)
                y0 = min(y0, y)
                x1 = max(x1, x)
                y1 = max(y1, y)
    share = count / float(width * height)
    box = None if count == 0 else (round(x0 / width, 3), round(y0 / height, 3),
                                   round(x1 / width, 3), round(y1 / height, 3))
    return count, share, box


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="append", required=True, help="名稱|r,g,b|容差")
    ap.add_argument("images", nargs="+")
    args = ap.parse_args()

    probes = []
    for raw in args.probe:
        name, rgb, tolerance = raw.split("|")
        probes.append((name, tuple(int(v) for v in rgb.split(",")), int(tolerance)))

    for path in args.images:
        print(f"── {path}")
        for name, color, tolerance in probes:
            count, share, box = probe(path, color, tolerance)
            print(f"   {name:12s} {count:7d} px（{share * 100:6.3f}%）  外接框 {box}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
