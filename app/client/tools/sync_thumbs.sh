#!/usr/bin/env bash
# **把設定頁卡片要用的縮圖烤進 Unity**（`Assets/Resources/backgrounds/`）。
#
# 為什麼要一支：真相的背景卡縮圖是 `<img src="../backgrounds/<id>/thumb.jpg">` ＋
# CSS 的 `object-fit: cover`（16:9 原圖塞進 72×42 的框 ⇒ 置中裁切、不是拉伸）。
# UGUI 的 `RawImage` 要自己做 uvRect 裁切，而**同一件事在烤的時候做一次最省**
# ——與頭像同一條路（`sync_avatars.sh` 也是烤 144×144 的白底 cover）。
#
# 兩個產物：
#   `backgrounds/<id>.png`  每個背景目錄的 `thumb.jpg` ⇒ 4× 的 288×168（設計 72×42）
#   `backgrounds/_plain.png`「無背景」那張卡沒有檔案 ⇒ 照 `styles.css` 的
#     `#background-list .character-card img.is-plain` 那兩條 60°／−60° 斜線紋自己畫
#     （`linear-gradient(60deg, transparent 44%, #d3e9f5 44% 56%, transparent 56%) 0 0/16px 14px`
#      ＋ `…(-60deg, #e2f1f9) 8px 0/16px 14px` 疊在 `#f7fcff` 上——斜線角度與色停都照 CSS 算）。
#
# 用法：bash tools/sync_thumbs.sh
# 產物進版控（與貼圖／頭像同一條紀律：打包進 Unity，不必等 webui 退役）。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
WS="$(cd "$REPO/../../.." && pwd)"
SRC="$(cd "$REPO/.." && pwd)/live2d/assets/backgrounds"
DST="$REPO/BlueAgentUnity/Assets/Resources/backgrounds"
PY="$WS/.venv-yolo/bin/python"

[ -d "$SRC" ] || { echo "找不到背景來源：$SRC"; exit 1; }
[ -x "$PY" ] || { echo "找不到 python（PIL）：$PY"; exit 1; }
mkdir -p "$DST"

"$PY" - "$SRC" "$DST" <<'PY'
import glob
import math
import os
import sys

from PIL import Image

src, dst = sys.argv[1], sys.argv[2]
W, H = 288, 168          # 設計 72×42 的 4 倍（畫面上縮回來仍銳利）


def cover(path, out):
    """`object-fit: cover` 的置中裁切（與 `sync_avatars.sh` 同一條路）。"""
    with Image.open(path) as raw:
        image = raw.convert("RGBA")
    scale = max(W / image.width, H / image.height)
    scaled = image.resize((max(W, round(image.width * scale)), max(H, round(image.height * scale))),
                          Image.Resampling.LANCZOS)
    left = (scaled.width - W) // 2
    top = (scaled.height - H) // 2
    cropped = scaled.crop((left, top, left + W, top + H))
    canvas = Image.new("RGB", (W, H), (255, 255, 255))      # `.character-card img { background: white }`
    canvas.paste(cropped, (0, 0), cropped)
    canvas.save(out, "PNG", optimize=True)


count = 0
for path in sorted(glob.glob(os.path.join(src, "*", "thumb.jpg"))):
    bid = os.path.basename(os.path.dirname(path))
    cover(path, os.path.join(dst, bid + ".png"))
    count += 1
    print(f"  背景 {bid} ⇒ backgrounds/{bid}.png")


def stripe(out):
    """`is-plain` 的斜線紋：兩條 60° 帶狀漸層，疊在 #f7fcff 上（CSS 是 16×14 一格）。

    ⚠️ 這一格是**設計 px**：烤出來的圖是 4 倍 ⇒ 磚子也是 4 倍（64×56），
    不然相位會整個跑掉（第一版就是這樣：格用 16 image px ⇒ 條紋對不上真相）。
    """
    scale = W / 72.0                       # ＝4
    tw, th = 16.0 * scale, 14.0 * scale
    base = (247, 252, 255)
    image = Image.new("RGB", (W, H), base)
    px = image.load()
    for y in range(H):
        for x in range(W):
            colour = list(base)
            for angle, offset, band in ((60.0, 0.0, (211, 233, 245)), (-60.0, 8.0, (226, 241, 249))):
                # CSS：`linear-gradient(角度, transparent 44%, 色 44% 56%, transparent 56%)`
                # 漸層線方向＝(sinθ, −cosθ)（畫面座標 y 往下），長度＝|w·sin|＋|h·cos|，
                # 沒有給位置 ⇒ **居中**（算相對格心的投影，再歸一到 0..1）。
                rad = math.radians(angle)
                dx, dy = math.sin(rad), -math.cos(rad)
                length = abs(tw * math.sin(rad)) + abs(th * math.cos(rad))
                lx = (x - offset * scale) % tw
                ly = y % th
                t = ((lx - tw / 2.0) * dx + (ly - th / 2.0) * dy) / length + 0.5
                if 0.44 <= t <= 0.56:
                    colour = list(band)
            px[x, y] = tuple(colour)
    image.save(out, "PNG", optimize=True)


stripe(os.path.join(dst, "_plain.png"))
count += 1
print("  「無背景」斜線紋 ⇒ backgrounds/_plain.png")
print(f"烤好 {count} 張（每張 {W}×{H}，白底 cover／斜線紋）")
PY

echo "背景縮圖 ⇒ $DST：$(ls "$DST" | wc -l) 張（$(du -sh "$DST" | cut -f1)）"
