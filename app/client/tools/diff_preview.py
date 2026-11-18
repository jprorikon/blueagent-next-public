#!/usr/bin/env python3
"""機械比對：同一場景、只換嘴型值的兩張預覽圖差在哪裡。

用法：.venv-cv/bin/python tools/diff_preview.py 圖A 圖B
判準：差異像素數 > 0，且差異集中在臉部（不是整張圖位移／整格變色）。
"""
import sys

import numpy as np
from PIL import Image


def load(path):
    return np.asarray(Image.open(path).convert("RGB"), dtype=int)


def main():
    if len(sys.argv) != 3:
        print("用法: diff_preview.py 圖A 圖B")
        return 2
    a, b = load(sys.argv[1]), load(sys.argv[2])
    if a.shape != b.shape:
        print(f"尺寸不同：{a.shape} vs {b.shape}")
        return 2
    delta = np.abs(a - b).max(axis=2)
    ys, xs = np.where(delta > 10)
    total = a.shape[0] * a.shape[1]
    print(f"影像 {a.shape[1]}x{a.shape[0]}｜差異像素 {len(xs)}／{total}（{len(xs) / total * 100:.3f}%）")
    if len(xs) == 0:
        print("⇒ 兩張一模一樣：這一步沒有生效")
        return 1
    print(f"差異範圍 x∈[{xs.min()},{xs.max()}] y∈[{ys.min()},{ys.max()}]"
          f"（畫面高 {a.shape[0]} ⇒ y 越小越靠上）")
    # 差異是否集中在一個小區塊（＝局部變動，不是整張位移）
    box_area = (xs.max() - xs.min() + 1) * (ys.max() - ys.min() + 1)
    print(f"差異外接矩形面積 {box_area}／{total}（{box_area / total * 100:.2f}%）"
          f"｜密度 {len(xs) / box_area * 100:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
