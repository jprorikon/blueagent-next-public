#!/usr/bin/env python3
"""**圓角量測**：量「那個元素的四個角到底有沒有被切掉、切掉多少」。

為什麼要一支（而不是用 VL 看）：圓角只有幾個 px，並排圖縮小看不出來，VL 也常猜錯（實測誤報過）。
這裡量的是**可複現的數字**：

  取「那個元素的四個角各 `radius × radius` 那一小格」，數裡面有幾 % 是**底色**（＝被切掉的部份）。
  * 直角 ⇒ **0%**（那一格全是元素本身的顏色）
  * 半徑 r 的圓角 ⇒ 那一格被切掉的面積 = r²(1 − π/4) ≈ **21.5%**

⚠️ 這個量法的前提是「底色與元素色不同」（例：白底上的深色氣泡）。給的矩形是**設計框座標**
（460×818）；Unity 的截圖也是 460×818 ⇒ 腳本自己按 `寬/460` 換算。

用法：
  python3 tools/corner_probe.py --rect 63,227,189,261 --fill 78,92,115 --radius 7 \
      Builds/webui-talk-chat.png Builds/unity-talk-chat-chat_messages.png
"""
import argparse
from PIL import Image

ap = argparse.ArgumentParser()
ap.add_argument("images", nargs="+")
ap.add_argument("--rect", help="元素矩形 x0,y0,x1,y1（設計框座標）；給了 --auto 就不必給")
ap.add_argument("--fill", required=True, help="元素本身的顏色 r,g,b")
ap.add_argument("--radius", type=float, default=7.0, help="CSS 的 border-radius（設計 px）")
ap.add_argument("--tol", type=float, default=30.0, help="顏色相近容忍（曼哈頓距離）")
ap.add_argument("--auto", action="store_true",
                help="⚠️ **先用顏色掃出元素的 bounding box**（不靠人猜的座標）——"
                     "兩邊的字寬常常差幾 px，用同一個矩形去套會把「量到框外」誤判成圓角被切掉")
ap.add_argument("--scan", help="--auto 的搜尋框 x0,y0,x1,y1（設計框座標）：元素一定落在裡面")
args = ap.parse_args()

fill = tuple(int(v) for v in args.fill.split(","))
r = args.radius


def near(im, x, y):
    px = im.getpixel((int(round(x)), int(round(y))))
    return abs(px[0] - fill[0]) + abs(px[1] - fill[1]) + abs(px[2] - fill[2]) <= args.tol


def bbox(im, scale, box):
    """在搜尋框裡掃出填色的 bounding box（設計框座標）。"""
    x0, y0, x1, y1 = (int(v * scale) for v in box)
    xs = []
    ys = []
    for y in range(y0, min(y1, im.height)):
        for x in range(x0, min(x1, im.width)):
            if near(im, x, y):
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return (min(xs) / scale, min(ys) / scale, (max(xs) + 1) / scale, (max(ys) + 1) / scale)


for path in args.images:
    im = Image.open(path).convert("RGB")
    scale = im.width / 460.0
    if args.auto:
        box = tuple(float(v) for v in (args.scan or "0,0,460,818").split(","))
        found = bbox(im, scale, box)
        if found is None:
            print(f"  {path}: 搜尋框裡找不到那個顏色（換 --fill／--scan）")
            continue
        x0, y0, x1, y1 = found
        print(f"  {path}（{im.size[0]}×{im.size[1]}，換算 {scale:.3f}×）"
              f"⇒ 掃到的矩形：{x0:.1f}, {y0:.1f} → {x1:.1f}, {y1:.1f}（寬 {x1 - x0:.1f} 高 {y1 - y0:.1f}）")
    else:
        x0, y0, x1, y1 = (float(v) for v in args.rect.split(","))
        print(f"  {path}（{im.size[0]}×{im.size[1]}，換算 {scale:.3f}×）")
    cells = {
        "左上": (x0, y0, x0 + r, y0 + r),
        "右上": (x1 - r, y0, x1, y0 + r),
        "左下": (x0, y1 - r, x0 + r, y1),
        "右下": (x1 - r, y1 - r, x1, y1),
    }
    print(f"  {path}（{im.size[0]}×{im.size[1]}，換算 {scale:.3f}×）")
    cut_total = 0.0
    for name, (cx0, cy0, cx1, cy1) in cells.items():
        total = 0
        fill_px = 0
        for gy in range(int(cy0 * scale), int(cy1 * scale)):
            for gx in range(int(cx0 * scale), int(cx1 * scale)):
                total += 1
                if near(im, gx, gy):
                    fill_px += 1
        cut = 100.0 * (total - fill_px) / total if total else 0.0
        cut_total += cut
        print(f"    {name}角：切掉 {cut:5.1f}%")
    avg = cut_total / 4
    verdict = "圓角（與 CSS 的 %.1f%% 相近）" % (100 * (1 - 3.141592653589793 / 4)) if avg > 8 else "**看起來是直角**（切掉 ≈ 0）"
    print(f"    ⇒ 平均切掉 {avg:.1f}% ⇒ {verdict}")
