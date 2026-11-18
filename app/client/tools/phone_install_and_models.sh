#!/usr/bin/env bash
# 手機側一次做兩件：① 裝最新 APK（md5 對得上才算）② 把聲紋質心 ＋ 被指名的 onnx 推進 Unity 版的 models/
#
# 為什麼要搬（2026-10-20）：舊 Kotlin app 的模型在 `/data/data/dev.deepsea.blueagent/files/models`，
# Unity 這一版讀 `/sdcard/Android/data/dev.deepsea.blueagent.unity/files/models`（persistentDataPath）
# ⇒ 不搬的話「①聲紋模型」只剩 `off`。只列「質心指名的那顆 onnx 也在」的檔（見 `WakeNet.CentroidOptions`）。
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH=/opt/blueagent-next/android-toolchain/sdk/platform-tools:$PATH
APP=dev.deepsea.blueagent.unity
APK=Builds/BlueAgentSpike.apk
SRC=../kws/models
REMOTE="/sdcard/Android/data/$APP/files/models"

# ⚠️ 裝置名可能含空白（`adb-…EWGzGE (2)._adb-tls-connect._tcp`）⇒ 用 tab 切欄位，不要用 awk 抓 $1
DEV=$(adb devices | grep -w device | cut -f1 | head -1)
[ -n "${DEV}" ] || { echo "✗ 沒有裝置"; exit 2; }
echo "  裝置：$DEV（$(adb -s "$DEV" shell getprop ro.product.model | tr -d '\r')）"

echo "── ① 裝 APK ──"
NAS_MD5=$(md5sum "$APK" | cut -d' ' -f1)
adb -s "$DEV" install -r "$APK" 2>&1 | tail -1
P=$(adb -s "$DEV" shell pm path "$APP" | tr -d '\r' | sed 's/package://')
GOT=$(adb -s "$DEV" shell md5sum "$P" | tr -d '\r' | awk '{print $1}')
echo "  NAS=$NAS_MD5／裝置=$GOT"
[ "$GOT" = "$NAS_MD5" ] || { echo "✗ 裝上去的不是這一顆 ⇒ 停"; exit 3; }
echo "  ✓ 裝置上就是剛驗過的那一顆"

echo "── ② 推聲紋檔（質心 json ＋ 被指名的 onnx）──"
adb -s "$DEV" shell mkdir -p "$REMOTE" >/dev/null 2>&1
for f in spk_centroid_campplus_zhcn.json spk_centroid_eres2net_base200k.json \
         spk_centroid_eres2net_base200k_live.json spk_centroid_eres2netv2.json; do
  adb -s "$DEV" push "$SRC/$f" "$REMOTE/" 2>&1 | tail -1
done
for f in sv/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx \
         sv/3dspeaker_speech_eres2net_base_200k_sv_zh-cn_16k-common.onnx \
         sv/3dspeaker_eres2netv2_zh_16k.onnx; do
  echo "  · $(basename "$f")（$(du -h "$SRC/$f" | cut -f1)）"
  adb -s "$DEV" push "$SRC/$f" "$REMOTE/" 2>&1 | tail -1
done
echo "  ── 推完的 models/ ──"
adb -s "$DEV" shell "ls -l $REMOTE" | tr -d '\r' | awk '{print "   ", $5, $8}'
