#!/usr/bin/env python3
"""**同一格設計座標的豎掃**：在一條（或幾條）x 上，把真相與 Unity 的每一行顏色並排印出來。

為什麼要有這一支（`color_probe`／`ink_bbox` 之外）：這兩支給的是**佔比**與**外接框**，
回答「有沒有一條壓條」夠用，但回答**「那條壓條是 2px 還是 3px、差在哪一行」**不夠——
那要一行一行看。壓條／下緣／1px 外框這一類差異，全都是「差幾行」的問題。

用法（**用 `.venv-yolo` 的 python**，本機沒有 PIL）：
    .venv-yolo/bin/python tools/row_scan.py Builds/webui-home.png Builds/unity-home-three_bubbles.png \\
        --x 200 --y 770,800 [--x 60]
* 座標一律給**設計格**（460×818，＝真相圖的像素座標）；Unity 那張（460×818，1:1）自動換算過去取最近點。
* 每一行印 `y 設計色 → unity 色`；只有**兩邊不同**的行會標 `≠`（相同的一律印 `=`，方便一眼掃過）。
"""
import argparse

from PIL import Image


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("truth")
    ap.add_argument("unity")
    ap.add_argument("--x", action="append", required=True, help="設計格 x（可多個）")
    ap.add_argument("--y", required=True, help="設計格 y 範圍 y0,y1")
    ap.add_argument("--all", action="store_true", help="每一行都印（預設只印不同的）")
    args = ap.parse_args()

    truth = Image.open(args.truth).convert("RGB")
    unity = Image.open(args.unity).convert("RGB")
    # 真相＝設計格本身；Unity 是同一版面的放大畫布 ⇒ 按比例取最近點。
    sx = unity.width / truth.width
    sy = unity.height / truth.height
    y0, y1 = (int(v) for v in args.y.split(","))

    for x_text in args.x:
        x = int(x_text)
        ux = min(unity.width - 1, int(round(x * sx)))
        print(f"── x={x}（unity x={ux}；放大 {sx:.4f}/{sy:.4f}）")
        for y in range(y0, y1 + 1):
            uy = min(unity.height - 1, int(round(y * sy)))
            a = truth.getpixel((x, y))
            b = unity.getpixel((ux, uy))
            mark = "=" if a == b else "≠"
            if mark == "≠" or args.all:
                print(f"  {mark} y={y:>4}  真相 {a}  →  unity {b}")
        if not args.all:
            print(f"  （上面只印不同的行；其餘 y={y0}..{y1} 兩邊相同）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
