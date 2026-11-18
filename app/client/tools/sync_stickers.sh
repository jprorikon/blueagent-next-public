#!/usr/bin/env bash
# **把貼圖打包進 Unity 專案**（`Assets/Resources/stickers/`）。
#
# 為什麼要一支：104 張貼圖是**產物**（`blueagent/stickers/build_stickers.py` 生成，
# webui 那一側由 `assets/webui/stickers.js` 記名）——不手抄、不進版控的第二份真相。
# 這一支把來源那些檔**攤平成 `<名字>.png`** 放進 Unity（名字本身就是 webui 的 key，
# JS 的物件鍵不會重複 ⇒ 攤平不會撞名；順序兩邊都用「名字排序」）。
#
# ⚠️ **來源是 `.webp`，這裡一定要轉 PNG**（2026-10-11 實測）：Unity **不吃 `.webp`**——
#    匯入器落在 `DefaultImporter`、`Resources.LoadAll<Texture2D>` 回 **0 張**
#    （畫面上一格貼圖都沒有，而文字 dump 照樣全綠：那一格只有「104 枚」這種字）。
#    貼圖是平塗色塊 ⇒ 轉 PNG 無損、反而更適合。
#
# 用法：bash tools/sync_stickers.sh
# 產物：`BlueAgentUnity/Assets/Resources/stickers/*.png`（**進版控**——owner 2026-10-11 定案：
#       貼圖打包進 Unity，不必等 webui 退役）。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
WS="$(cd "$REPO/../../.." && pwd)"
SRC="$(cd "$REPO/.." && pwd)/live2d/assets/stickers"
DST="$REPO/BlueAgentUnity/Assets/Resources/stickers"
PY="$WS/.venv-yolo/bin/python"

[ -d "$SRC" ] || { echo "找不到貼圖來源：$SRC"; exit 1; }
[ -x "$PY" ] || { echo "找不到 python（PIL）：$PY"; exit 1; }
rm -rf "$DST" && mkdir -p "$DST"
"$PY" - "$SRC" "$DST" <<'PY'
import glob, os, sys
from PIL import Image
src, dst = sys.argv[1], sys.argv[2]
count = 0
for path in sorted(glob.glob(os.path.join(src, "*", "*.webp"))):
    name = os.path.splitext(os.path.basename(path))[0]
    with Image.open(path) as image:
        image.convert("RGBA").save(os.path.join(dst, name + ".png"), "PNG", optimize=True)
    count += 1
print(f"轉檔 {count} 張 webp → png")
PY
echo "貼圖 ⇒ $DST：$(ls "$DST" | wc -l) 張（$(du -sh "$DST" | cut -f1)）"
