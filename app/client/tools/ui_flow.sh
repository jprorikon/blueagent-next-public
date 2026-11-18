#!/usr/bin/env bash
# **互動流程自驅測試**：把一組節點名推進去，app 開機後自己依序點（`AppShell.FlowTick`），判據全在 logcat。
#
# 為什麼要這一條（owner 2026-10-14：「Unity 不能自動化測試麼」）：
#   真機驗「點一下」本來要人手點＋截圖比對，一次要重建 APK（約 9 分鐘）。
#   這一條＝**裝一次包，之後每次換一組流程**：`am start` → 自己點 → 每步印一行 → 約 15 秒一輪，
#   而且不看畫面（判據是 log 的 `[flow]` 行）⇒ 可以放在腳本／排程裡跑。
#
# 用法：
#   bash tools/ui_flow.sh "NavButton;Tile3_settings;SettingsClose"
#   bash tools/ui_flow.sh "NavButton;Tile3_settings" --expect "屏=settings"
# 選項：--expect <子字串>（可多個，全部都要在 `[flow]` 行裡出現才算過）
set -uo pipefail
D="${ADB_DEV:-192.168.50.22:36195}"
PKG=dev.deepsea.blueagent.unity
ADB="adb -s $D"
STEPS="${1:?用法: ui_flow.sh \"NavButton;Tile3_settings;SettingsClose\" [--expect <子字串>]}"
shift || true

EXPECT=()
SETTLE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --expect) EXPECT+=("$2"); shift 2 ;;
    # 跑完「完成」之後**再多等幾秒才收一次 log**——橋的回覆（送話之後那幾秒）才進得來。
    --settle) SETTLE="$2"; shift 2 ;;
    *) echo "不認得的參數：$1"; exit 2 ;;
  esac
done

TMP="$(mktemp)"
printf '%s' "$STEPS" | tr ';' '\n' > "$TMP"
echo "── 流程：$STEPS"
if ! $ADB push "$TMP" "/sdcard/Android/data/$PKG/files/ui_flow.txt" >/dev/null 2>&1; then
  echo "✗ 推不進流程檔（app 裝了嗎？路徑對嗎？）"; rm -f "$TMP"; exit 2
fi
rm -f "$TMP"

$ADB logcat -c >/dev/null 2>&1
$ADB shell am force-stop $PKG >/dev/null 2>&1
sleep 1
$ADB shell monkey -p $PKG -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1

LOG=""
# ⚠️ 迴圈**要夠長**：短流程（三五步）20 次（≈40 秒）綽綽有餘，但「全部頁面跑一遍」那條
#    （`ui_flow_all.sh`，30+ 步 × 0.8 秒）會超過 ⇒ 40 次（≈80 秒）當上限。
for i in $(seq 1 40); do
  # ⚠️ 一起收 `[ui]`／`[shell]`／`[bridge]`：`--expect` 要能對「疊層：settings」這種**行為**行、
  #    換屏那一行（`[shell] 換屏：game`，只收前兩族會讓那種期待永遠落空），以及橋那一條
  #    （`[bridge]`——送話／收幀／錯誤都在那裡）。
  LOG="$($ADB logcat -d -s Unity 2>/dev/null | grep -aE '\[flow\]|\[ui\]|\[shell\]|\[bridge\]')"
  echo "$LOG" | grep -aq '\[flow\] 完成' && break
  sleep 2
done

# 橋的回覆要幾秒 ⇒ 「完成」之後再收一次（`--settle <秒>`）。
if [ "${SETTLE:-0}" -gt 0 ]; then
  sleep "$SETTLE"
  LOG="$($ADB logcat -d -s Unity 2>/dev/null | grep -aE '\[flow\]|\[ui\]|\[shell\]|\[bridge\]')"
fi

echo "$LOG" | grep -av '載入' | head -20

CODE=0
if ! echo "$LOG" | grep -aq '\[flow\] 完成'; then
  echo "✗ 沒跑到「完成」（app 沒起來／流程檔沒讀到）"; CODE=1
fi
for want in "${EXPECT[@]:-}"; do
  [ -z "$want" ] && continue
  if echo "$LOG" | grep -aq -- "$want"; then
    echo "✓ 期待命中：$want"
  else
    echo "✗ 期待沒出現：$want"; CODE=1
  fi
done
echo "── ui_flow 結束（exit=$CODE）──"
exit $CODE
