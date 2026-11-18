#!/usr/bin/env bash
# 重啟 app 驗「橋 URL 真的換成 tailscale 且生效」：
#   ① 重啟後 app 對橋的連線，**peer 應該是手機的 tailscale IP**（100.64.0.1），不是 192.168.50.22
#   ② app log 要有「橋在線」＋「推設定 ok」
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH=/opt/blueagent-next/android-toolchain/sdk/platform-tools:$PATH
APP=dev.deepsea.blueagent.unity
PHONE_TS="${PHONE_TS:-100.64.0.1}"
# ⚠️ 裝置名可能含空白 ⇒ tab 切欄位
DEV=$(adb devices | grep -w device | cut -f1 | head -1)
A() { adb -s "$DEV" "$@"; }
echo "  裝置=$DEV"
A shell am force-stop $APP >/dev/null 2>&1
sleep 2
A logcat -c >/dev/null 2>&1
ENTRY=$(A shell cmd package resolve-activity --brief $APP | tail -1 | tr -d '\r' | tail -1)
A shell am start -n "$ENTRY" >/dev/null 2>&1
echo "  等 20 秒讓它連橋…"
sleep 20
echo "── app log（橋）──"
A logcat -d 2>/dev/null | tr -d '\r' | grep -E "橋在線|推設定|端末設定|連不上|error" | tail -6
echo "── NAS 這一側 :8900 的連線 peer ──"
ss -tn 2>/dev/null | awk '/:8900/ {print "   ", $4, "←", $5}' | sort -u
echo "── 判定 ──"
if ss -tn 2>/dev/null | grep ':8900' | grep -q "$PHONE_TS"; then
  echo "  ✓ 有來自 tailscale（$PHONE_TS）的連線"
else
  echo "  ✗ 沒看到 tailscale 那條（可能還在用舊位址）"
fi
if ss -tn 2>/dev/null | grep ':8900' | grep -q "192\.168\.0\.22"; then
  echo "  ⚠️ 同時還有 192.168.50.22 的連線（舊連線還沒收掉，或設定沒生效）"
fi
