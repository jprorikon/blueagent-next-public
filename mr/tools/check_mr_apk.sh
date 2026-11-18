#!/usr/bin/env bash
# MR（Quest 3）包的 manifest 閘門：建完、侧載之前，用 aapt2 機械比對合併後的 manifest。
# 手機包由 app/client/tools/check_phone_apk.sh 檢查；這支只負責 MR。
# 用法：bash mr/tools/check_mr_apk.sh [APK]（預設 mr/Builds/BlueAgentMR.apk）
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
MR_REPO="$(cd "$HERE/.." && pwd)"
APK="${1:-$MR_REPO/Builds/BlueAgentMR.apk}"
[ -f "$APK" ] || { echo "✗ 找不到 $APK"; exit 2; }

AAPT2="${AAPT2:-$(ls /Applications/Unity/Hub/Editor/*/PlaybackEngines/AndroidPlayer/SDK/build-tools/*/aapt2 2>/dev/null | tail -1)}"
[ -x "$AAPT2" ] || AAPT2="$(command -v aapt2 || true)"
[ -x "$AAPT2" ] || { echo "✗ 找不到 aapt2（設 AAPT2=<路徑>）"; exit 2; }

MANIFEST="$("$AAPT2" dump xmltree --file AndroidManifest.xml "$APK")" || { echo "✗ aapt2 讀不了 $APK"; exit 2; }
FAIL=0
must()    { if grep -q -- "$1" <<<"$MANIFEST"; then echo "✓ 有 $2"; else echo "✗ 缺 $2（$1）"; FAIL=1; fi; }
mustnot() { if grep -q -- "$1" <<<"$MANIFEST"; then echo "✗ 不該有 $2（$1）"; FAIL=1; else echo "✓ 沒有 $2"; fi; }

echo "── $APK ──"
must    '"dev.deepsea.blueagent.mr"'            "MR 包名"
must    'com.oculus.intent.category.VR'         "Quest 入口分類（沒有＝被當成 2D 面板 app）"
must    'com.oculus.permission.USE_SCENE'       "空間資料權限（平面／Space Setup）"
must    'com.oculus.supportedDevices'           "Quest 裝置清單"
must    'android.permission.INTERNET'           "網路權限"
must    'com.oculus.permission.HAND_TRACKING'   "手部追蹤權限"
must    'oculus.software.handtracking'          "手部追蹤 uses-feature"
must    'android.permission.RECORD_AUDIO'       "麥克風"
mustnot 'android.intent.category.HOME'          "主屏 launcher（手機的 androidlib 混進來了）"
mustnot 'SeeAccessibilityService'               "無障礙服務（手機的 androidlib 混進來了）"
mustnot 'dev.deepsea.blueagent.shell'           "手機的原生殼（androidlib）"
if [ "$FAIL" != 0 ]; then
  echo "✗ manifest 不合格 ⇒ 不要侧載"
  exit 1
fi
echo "✓ manifest 合格"
