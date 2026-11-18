#!/usr/bin/env bash
# 手機 APK 的 manifest 閘門：手機入口與權限必須存在，Quest／OpenXR 不得串進手機包。
# 用法：bash app/client/tools/check_phone_apk.sh [APK]（預設 app/client/Builds/BlueAgentNext.apk）
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CLIENT_REPO="$(cd "$HERE/.." && pwd)"
APK="${1:-$CLIENT_REPO/Builds/BlueAgentNext.apk}"
[ -f "$APK" ] || { echo "✗ 找不到 $APK"; exit 2; }

AAPT2="${AAPT2:-$(ls /Applications/Unity/Hub/Editor/*/PlaybackEngines/AndroidPlayer/SDK/build-tools/*/aapt2 2>/dev/null | tail -1)}"
[ -x "$AAPT2" ] || AAPT2="$(command -v aapt2 || true)"
[ -x "$AAPT2" ] || { echo "✗ 找不到 aapt2（設 AAPT2=<路徑>）"; exit 2; }

MANIFEST="$("$AAPT2" dump xmltree --file AndroidManifest.xml "$APK")" || { echo "✗ aapt2 讀不了 $APK"; exit 2; }
FAIL=0
must()    { if grep -q -- "$1" <<<"$MANIFEST"; then echo "✓ 有 $2"; else echo "✗ 缺 $2（$1）"; FAIL=1; fi; }
mustnot() { if grep -q -- "$1" <<<"$MANIFEST"; then echo "✗ 不該有 $2（$1）"; FAIL=1; else echo "✓ 沒有 $2"; fi; }

echo "── $APK ──"
must    '"dev.deepsea.blueagent.next"'          "手機包名"
must    'android.intent.category.HOME'          "主屏 launcher（手機的 androidlib 在）"
mustnot 'com.oculus.intent.category.VR'         "Quest 入口分類"
mustnot 'com.unity.xr.openxr'                   "OpenXR 套件標記"
mustnot 'com.oculus.permission.USE_SCENE'       "空間資料權限"
mustnot 'com.oculus.supportedDevices'           "Quest 裝置清單"
mustnot 'com.oculus.permission.HAND_TRACKING'   "手部追蹤權限"
if [ "$FAIL" != 0 ]; then
  echo "✗ manifest 不合格 ⇒ 不要安裝"
  exit 1
fi
echo "✓ manifest 合格"
