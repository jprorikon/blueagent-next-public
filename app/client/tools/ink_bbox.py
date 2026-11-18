#!/usr/bin/env python3
"""**墨跡 bbox 量測**（第三路判據：文字 A/B 之外、`rect_audit` 之外）。

為什麼要有這一支（2026-10-12 實測踩到）：`rect_audit` 比的是兩邊的 **rect**——真相那一側的
行內元素（`span`）是**墨跡框**（收縮到字寬），Unity 那一側是**被 `Weight`／`Stretch` 撐開的節點框**。
兩者左上角一比只差 1px（`span|會話|409 22 21 11` ↔ Unity `rect 410,15 32x24`），**字卻差了 6.9px**
（`text-align:center` 對上 CSS 的 `left`）⇒ Δx 完全看不出來。行內的短標籤、鈕上的字、
`right` 對齊的小字（`.stage-serial`）都要看**墨跡**，不是看框。

用法（**用 `.venv-yolo` 的 python**，本機沒有 PIL）：
    .venv-yolo/bin/python tools/ink_bbox.py --crop 372,12,456,48 --color 35,83,125 --tol 45 \
        Builds/webui-home.png Builds/unity-home--three_bubbles.png
    .venv-yolo/bin/python tools/ink_bbox.py --crop 372,12,456,48 --dark 190 \
        Builds/webui-home.png Builds/screen-mouth0.png

* `--crop`：**設計座標**（460×818 那一格）的 `x0,y0,x1,y1`——只量這一格，避免同名同色
  （例：`#23537d` 的品牌字）被一起框進去。
* `--color`：目標色 `r,g,b`（＋`--tol`，預設 30）＝墨跡核心的顏色（例：`#23537d`）。
* `--dark`：改用亮度門檻（`0.299r+0.587g+0.114b < N`）＝「比底深的都算」，
  適合底是亮色而字有抗鋸齒的場合。兩個擇一。
* 兩張圖尺寸可以不同（真相 460×818、Unity 460×818（2026-10-12 起與真相 1:1））⇒ 內部按 `寬/460` 換算，
  輸出**一律是設計座標**，兩邊可以直接相減。
* 判讀：**位置**看 bbox 的 x／y（Δ ≤ 1px 算對得上），**大小**看寬高（抗鋸齒會讓 ±1px 是常態）。
"""
import argparse
import sys

from PIL import Image


def bbox(image, scale, crop, test):
    x0, y0, x1, y1 = crop
    xs, ys = [], []
    for y in range(int(y0 * scale), int(y1 * scale)):
        for x in range(int(x0 * scale), int(x1 * scale)):
            r, g, b = image.getpixel((x, y))
            if test(r, g, b):
                xs.append(x / scale)
                ys.append(y / scale)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="+")
    ap.add_argument("--crop", required=True, help="設計座標 x0,y0,x1,y1（460 寬那一格）")
    ap.add_argument("--color", default="", help="r,g,b；墨跡核心色")
    ap.add_argument("--tol", type=int, default=30)
    ap.add_argument("--dark", type=int, default=0, help="亮度門檻（< N 算墨跡）")
    args = ap.parse_args()

    crop = [float(v) for v in args.crop.split(",")]
    if len(crop) != 4:
        raise SystemExit("--crop 要 x0,y0,x1,y1")
    if args.color:
        r0, g0, b0 = (int(v) for v in args.color.split(","))

        def test(r, g, b):
            return abs(r - r0) <= args.tol and abs(g - g0) <= args.tol and abs(b - b0) <= args.tol
    elif args.dark:
        def test(r, g, b):
            return 0.299 * r + 0.587 * g + 0.114 * b < args.dark
    else:
        raise SystemExit("要給 --color 或 --dark")

    for path in args.images:
        image = Image.open(path).convert("RGB")
        scale = image.size[0] / 460.0
        box = bbox(image, scale, crop, test)
        if box is None:
            print(f"{path}\n    這一格裡找不到那樣的像素")
            continue
        x0, y0, x1, y1 = box
        print(f"{path}\n    設計座標 x {x0:.1f}..{x1:.1f}  y {y0:.1f}..{y1:.1f}"
              f"   （寬 {x1 - x0:.1f} 高 {y1 - y0:.1f}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
