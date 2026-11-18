#!/usr/bin/env python3
"""真相 vs Unity 的**差異熱點盤點**：把兩張圖縮到同一格、算逐點色差，回報「差在哪裡」。

為什麼需要它：文字 A/B 只看得到字，`rect_audit` 只看得到盒子——**「少畫了一顆陰影／一條壓條」
兩者都抓不到**（那不是節點，是像素）。這支把差異區塊找出來，再回頭對 DOM／CSS 點名。

用法：python3 tools/diff_regions.py <truth.png> <unity.png> [--thresh 12] [--min-area 40]
"""
import sys
from PIL import Image
import numpy as np


def regions(mask, min_area):
    """連通區塊（4 鄰接）＋外接框；純 numpy 洪水填充，圖不大所以夠快。"""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    out = []
    for y in range(h):
        for x in range(w):
            if not mask[y, x] or seen[y, x]:
                continue
            stack = [(y, x)]
            seen[y, x] = True
            ys, xs = [], []
            while stack:
                cy, cx = stack.pop()
                ys.append(cy)
                xs.append(cx)
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            if len(ys) >= min_area:
                out.append((min(xs), min(ys), max(xs), max(ys), len(ys)))
    return sorted(out, key=lambda r: -r[4])


def main():
    truth_png, unity_png = sys.argv[1], sys.argv[2]
    thresh = 12
    min_area = 40
    args = sys.argv[3:]
    for i, a in enumerate(args):
        if a == "--thresh" and i + 1 < len(args):
            thresh = int(args[i + 1])
        if a == "--min-area" and i + 1 < len(args):
            min_area = int(args[i + 1])

    ref = (460, 818)
    t = Image.open(truth_png).convert("RGB")
    u = Image.open(unity_png).convert("RGB")
    if t.size != ref:
        t = t.resize(ref, Image.LANCZOS)
    if u.size != ref:
        u = u.resize(ref, Image.LANCZOS)
    ta = np.asarray(t, dtype=np.int16)
    ua = np.asarray(u, dtype=np.int16)
    d = np.abs(ta - ua).max(axis=2)
    mask = d > thresh
    print(f"差異點（>{thresh}）: {int(mask.sum())} / {mask.size}  ({100.0 * mask.sum() / mask.size:.2f}%)")
    for x0, y0, x1, y1, n in regions(mask, min_area):
        pat = " ".join(f"({ta[y, x][0]},{ta[y, x][1]},{ta[y, x][2]})→({ua[y, x][0]},{ua[y, x][1]},{ua[y, x][2]})"
                       for y, x in [(y0 + (y1 - y0) // 2, x0 + (x1 - x0) // 2)])
        print(f"  x {x0:3d}..{x1:3d}  y {y0:3d}..{y1:3d}  {x1 - x0 + 1:3d}×{y1 - y0 + 1:3d}  n={n:5d}  中心色 {pat}")


if __name__ == "__main__":
    main()
