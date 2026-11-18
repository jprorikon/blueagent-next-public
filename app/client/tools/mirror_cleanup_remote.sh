#!/usr/bin/env bash
# 在**建置機**上跑：把同步根目錄裡「倉庫已經沒有」的檔案清掉——deploy_unity.sh 的鏡像同步刪除半邊。
#
#   用法（由 deploy_unity.sh 自動呼叫，通常不用手動）：
#     bash mirror_cleanup_remote.sh <REMOTE_DIR> <FILE_LIST> [--dry-run]
#       REMOTE_DIR  建置工作區，例：~/blueagent-unity
#       FILE_LIST   這次要同步過去的檔案清單（相對路徑，一行一個；deploy_unity.sh 用 find 產生）
#       --dry-run   只印要刪什麼，不動手
#
# 為什麼需要它（兩次真踩）：
#   · 2026-10-16：作廢的自訂 AndroidManifest 從倉庫刪了、Mac 上還在 ⇒ 取代 Unity 產的 launcher，
#     裝上去 `am start` 說 Activity 不存在（入口整個不見）。
#   · 2026-10-31：`UiBuilder.StickerMarkup` 從倉庫移除，但 Mac 上三支舊殼畫面（10-18 的版本）還在
#     ⇒ 編譯閘門 `error CS0117`。查了半天程式碼，其實是工作區有孤兒。
#
# 鐵律：
#   1. **只刪「Mac 有、這次清單沒有」的檔**。清單是唯一真相，不做任何推測、不碰清單裡的東西。
#   2. **`.meta` 不能盲刪**：倉庫根本不追蹤 `.meta`（Unity 在 Mac 自己生）。
#      所以只有「本體也被刪／本體早就不存在」時才連 `.meta` 一起刪；本體還在就留著（資料夾同理）。
#   3. **PRESERVE 清單**＝建置機自產、倉庫裡沒有的東西：
#      `BlueAgentUnity/Assets/Scenes/Next.unity` 由 `HeadlessBuild` 在建置機現場生成，不在版本庫裡。
#      MR 已拆到獨立構建工作區，手機鏡像不再保留 MR.unity 或任何 Quest 孤兒。
#   4. 只清空目錄（`-empty -delete`），非空目錄一個都不動。
set -uo pipefail

REMOTE="${1:?用法: mirror_cleanup_remote.sh <REMOTE_DIR> <FILE_LIST> [--dry-run]}"
LIST="${2:?缺少檔案清單}"
DRY="${3:-}"

PRESERVE=(
  "BlueAgentUnity/Assets/Scenes/Next.unity"
)
ROOTS=(shared BlueAgentUnity/Assets BlueAgentUnity/Packages BlueAgentUnity/ProjectSettings BlueAgentUnity/Keys fixtures)

cd "$REMOTE" || { echo "✗ 進不去 $REMOTE"; exit 2; }
[ -f "$LIST" ] || { echo "✗ 找不到清單 $LIST"; exit 2; }

MAC_NOW="$(mktemp -t ba-mac-files)"
ORPHANS="$(mktemp -t ba-orphans)"
trap 'rm -f "$MAC_NOW" "$ORPHANS"' EXIT

# 兩邊都用同一套排序（LC_ALL=C），comm 才對得起來
find "${ROOTS[@]}" -type f 2>/dev/null | LC_ALL=C sort > "$MAC_NOW"
LC_ALL=C sort "$LIST" -o "$LIST.sorted"
LC_ALL=C comm -13 "$LIST.sorted" "$MAC_NOW" > "$ORPHANS"
rm -f "$LIST.sorted"

kept_meta=0
deleted=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  # 保留清單（建置機自產）
  skip=0
  for p in "${PRESERVE[@]}"; do [ "$f" = "$p" ] && skip=1; done
  [ "$skip" = 1 ] && continue
  # .meta：本體還在就留（倉庫不追蹤 .meta，Mac 自己生）
  case "$f" in
    *.meta)
      base="${f%.meta}"
      if [ -e "$base" ]; then kept_meta=$((kept_meta + 1)); continue; fi
      ;;
  esac
  if [ "$DRY" = "--dry-run" ]; then
    echo "  DEL $f"
  else
    rm -f "$f"
  fi
  deleted=$((deleted + 1))
done < "$ORPHANS"

if [ "$DRY" != "--dry-run" ]; then
  find "${ROOTS[@]}" -mindepth 1 -type d -empty -delete 2>/dev/null
  echo "  鏡像清理：刪 $deleted 個孤兒檔／空目錄（保留 $kept_meta 個「本體還在」的 .meta）"
else
  echo "  鏡像清理（dry-run）：會刪 $deleted 個（保留 $kept_meta 個「本體還在」的 .meta）"
fi
