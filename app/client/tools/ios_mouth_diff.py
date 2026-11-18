"""比較 iOS 模擬器連續截圖：嘴部區域有沒有在動（機械判據，不靠眼睛）。

用法：
  .venv-yolo/bin/python tools/ios_mouth_diff.py Builds/ios-m1.png Builds/ios-m2.png ...

輸出：
  每對圖的「差異像素數」與差異的**外接矩形**（相對整張圖的座標），以及嘴部帶狀區域內的差異數。
  判讀：播 TTS 期間若嘴在動，差異會**集中在臉下半（嘴）那一小塊**，而且逐對都出現；
  若整張都在動（背景/立繪抖動），外接矩形會很大 ⇒ 不能算「嘴在動」。
"""
import sys
from PIL import Image
import numpy as np

THRESHOLD = 24          # 逐通道差 > 這個才算「有差」（壓掉 AA/編碼噪聲）


def load(path):
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.int16)


def bbox(mask):
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def main(paths):
    imgs = [load(p) for p in paths]
    h, w, _ = imgs[0].shape
    print(f"圖 {w}x{h}／{len(imgs)} 張")
    for i in range(len(imgs) - 1):
        a, b = imgs[i], imgs[i + 1]
        diff = np.abs(a - b).max(axis=2)
        mask = diff > THRESHOLD
        n = int(mask.sum())
        box = bbox(mask)
        # 嘴部帶狀：立繪舞台大約在 y 0.18–0.62，嘴在臉部中心偏下 ⇒ 取 y 0.22–0.42、x 0.28–0.72
        y0, y1 = int(h * 0.22), int(h * 0.42)
        x0, x1 = int(w * 0.28), int(w * 0.72)
        band = mask[y0:y1, x0:x1]
        print(f"{paths[i]} → {paths[i+1]}: 差異 {n} px"
              f"／外接 {box}／嘴帶內 {int(band.sum())} px"
              f"／最大差 {int(diff.max())}")


if __name__ == "__main__":
    main(sys.argv[1:])
