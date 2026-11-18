#!/usr/bin/env bash
# 把 `Builds/BlueAgentSpike.apk` 裝到 owner 手機（adb over Wi-Fi），並驗：
#   ① 裝置上的 base.apk md5 ＝ NAS 這一顆（「裝上去的就是剛驗過的那一顆」）
#   ② 起得來（用系統解析的入口）＋ 新包的指紋（螢幕方向鎖豎屏／殼就緒）
#   ③ 抓一張畫面回 /tmp
#
# 用法：bash tools/install_phone_apk.sh
# ⚠️ 手機的無線偵錯關掉時 adb 會斷（`adb devices` 空、`adb connect <ip>:<port>` 被拒）
#    ⇒ 那是手機側的事，叫他重開「無線偵錯」再跑。
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH=/opt/blueagent-next/android-toolchain/sdk/platform-tools:$PATH
APP=dev.deepsea.blueagent.unity
APK=Builds/BlueAgentSpike.apk
NAS_MD5=$(md5sum "$APK" | cut -d' ' -f1)
echo "  NAS APK：$(stat -c %s "$APK") bytes／md5 ${NAS_MD5}"

# ⚠️ 裝置名可能含空白（`adb-…EWGzGE (2)._adb-tls-connect._tcp`）⇒ 用 tab 切欄位，不要用 awk 抓 $1
DEV=$(adb devices | grep -w device | cut -f1 | head -1)
if [ -z "${DEV}" ]; then
  echo "  ✗ 沒有連上的裝置（手機的無線偵錯大概關了）"; adb devices; exit 2
fi
echo "  目標裝置：${DEV}（$(adb -s "$DEV" shell getprop ro.product.model | tr -d '\r')）"

echo "── 裝（-r 覆蓋，保留資料）──"
adb -s "$DEV" install -r "$APK" 2>&1 | tail -2

P=$(adb -s "$DEV" shell pm path "$APP" | tr -d '\r' | sed 's/package://')
GOT=$(adb -s "$DEV" shell md5sum "$P" | tr -d '\r' | awk '{print $1}')
echo "  裝置上 base.apk md5：${GOT}"
[ "$GOT" = "$NAS_MD5" ] && echo "  ✓ 裝置上的就是剛驗過的那一顆" || { echo "  ✗ md5 不一致 ⇒ 不要當成新版"; exit 3; }

echo "── 起 app ──"
adb -s "$DEV" shell pm grant "$APP" android.permission.POST_NOTIFICATIONS 2>/dev/null || true
adb -s "$DEV" logcat -c >/dev/null 2>&1
ENTRY=$(adb -s "$DEV" shell cmd package resolve-activity --brief "$APP" | tail -1 | tr -d '\r')
echo "  入口=${ENTRY}"
adb -s "$DEV" shell am start -n "$ENTRY" >/dev/null 2>&1
sleep 14
echo "── 新包指紋 ──"
adb -s "$DEV" logcat -d 2>/dev/null | tr -d '\r' | grep -E "EventSystem：建立|螢幕方向鎖豎屏|殼就緒|聲紋" | tail -5
echo "  焦點：$(adb -s "$DEV" shell dumpsys window 2>/dev/null | tr -d '\r' | grep -m1 mCurrentFocus)"
echo "  方向：$(adb -s "$DEV" shell dumpsys window 2>/dev/null | tr -d '\r' | grep -m1 -oE 'mDisplayRotation=[A-Z_0-9]+')｜$(adb -s "$DEV" shell wm size | tr -d '\r')"
adb -s "$DEV" shell screencap -p /sdcard/after-install.png >/dev/null 2>&1
adb -s "$DEV" pull /sdcard/after-install.png /tmp/after-install.png >/dev/null 2>&1
adb -s "$DEV" shell rm -f /sdcard/after-install.png
echo "  截圖：/tmp/after-install.png"
