#!/usr/bin/env bash
# **把對話氣泡的頭像打包進 Unity**（`Assets/Resources/avatars/<角色 id>.png`＋`_fallback.png`）。
#
# 為什麼要一支：真相的頭像是 CSS 畫的——
#   `.history-message.aster::before { top:2px; left:-46px; width:36px; height:36px; border-radius:50%;
#     background: #fff var(--spk-avatar, url('../assets/aster_icon_normal.png')) center/cover }`
#   （`styles.css:550`），而 `--spk-avatar` 逐條訊息指向
#   `characters/<id>/animated-normal.png`（`app.js:1874`，由 `entry.character` 決定）
#   ⇒ **每一則訊息都可能不同角色**，圖必須跟著角色走。
#
# owner 2026-10-11 定案：**打包進 Unity**（同貼圖那一條）。
#
# ⚠️ **在這一側先把圓形烤進 PNG**：那一格是 36×36 的圓形頭像，用 PIL 把
#    「白底 ＋ cover 縮放置中裁切 ＋ 圓形 alpha」一次烤好 ⇒ 執行期就是一個 `RawImage`
#    （不必在 Unity 裡疊 `Mask`／模板，少一層坑）。像素結果與上面那條 CSS 等價。
# ⚠️ 來源那幾張**不是正方形**（aster 695×700、nori 768×800…）⇒ 一定要照 `cover` 的規則
#    （等比縮到**蓋滿**再置中裁切），不是直接拉伸。
#
# 用法：bash tools/sync_avatars.sh
# 產物：`BlueAgentUnity/Assets/Resources/avatars/*.png`（**進版控**）。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
WS="$(cd "$REPO/../../.." && pwd)"
SRC="$(cd "$REPO/.." && pwd)/live2d/assets"
DST="$REPO/BlueAgentUnity/Assets/Resources/avatars"
PY="$WS/.venv-yolo/bin/python"

[ -d "$SRC/characters" ] || { echo "找不到角色來源：$SRC/characters"; exit 1; }
[ -x "$PY" ] || { echo "找不到 python（PIL）：$PY"; exit 1; }
rm -rf "$DST" && mkdir -p "$DST"

"$PY" - "$SRC" "$DST" <<'PY'
import glob
import os
import sys

from PIL import Image, ImageDraw

src, dst = sys.argv[1], sys.argv[2]
# 設計尺寸 36（`.history-message.aster::before` 的 36×36）⇒ 烤 4 倍解析度，畫面上縮回來仍銳利。
SIZE = 144


def bake(path, out):
    with Image.open(path) as raw:
        image = raw.convert("RGBA")
    # `center/cover`：等比縮到**蓋滿**那個方框，再置中裁切（不是拉伸）。
    scale = max(SIZE / image.width, SIZE / image.height)
    scaled = image.resize((max(SIZE, round(image.width * scale)), max(SIZE, round(image.height * scale))),
                          Image.Resampling.LANCZOS)
    left = (scaled.width - SIZE) // 2
    top = (scaled.height - SIZE) // 2
    cropped = scaled.crop((left, top, left + SIZE, top + SIZE))
    # `background: #fff` ⇒ 先把圖疊在白底上（來源有透明區）。
    canvas = Image.new("RGB", (SIZE, SIZE), (255, 255, 255))
    canvas.paste(cropped, (0, 0), cropped)
    # `border-radius: 50%` ⇒ 圓形 alpha（4× 解析度下把邊緣畫圓一點）。
    mask = Image.new("L", (SIZE * 4, SIZE * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, SIZE * 4 - 1, SIZE * 4 - 1), fill=255)
    mask = mask.resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    out_image = canvas.convert("RGBA")
    out_image.putalpha(mask)
    out_image.save(out, "PNG", optimize=True)


count = 0
for path in sorted(glob.glob(os.path.join(src, "characters", "*", "animated-normal.png"))):
    cid = os.path.basename(os.path.dirname(path))
    bake(path, os.path.join(dst, cid + ".png"))
    count += 1
    print(f"  角色 {cid} ⇒ avatars/{cid}.png")
fallback = os.path.join(src, "assets", "aster_icon_normal.png")
if os.path.exists(fallback):
    bake(fallback, os.path.join(dst, "_fallback.png"))
    count += 1
    print("  舊紀錄沒有角色欄位時的退路 ⇒ avatars/_fallback.png")
print(f"烤好 {count} 張頭像（每張 {SIZE}×{SIZE}，白底＋圓形 alpha）")
PY

echo "頭像 ⇒ $DST：$(ls "$DST" | wc -l) 張（$(du -sh "$DST" | cut -f1)）"
