#!/usr/bin/env bash
# 無頭建 **blueagent-next 的雙端包**（Android／arm64／IL2CPP）＋把 APK 拉回 NAS。
#
# 舊橋 Spike 包（`build_android.sh`）已於 2026-10-31 隨舊殼移除；這支是唯一的 Android 出包入口
# （`dev.deepsea.blueagent.next`）、不同的輸出檔名 ⇒ 兩個包能同時裝在同一支手機上。
#
# 用法：bash blueagent-next/app/client/tools/build_next_android.sh
# 前置：`tools/deploy_unity.sh`（推原始碼）＋ `tools/check_unity_compile.sh` exit 0。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
UNITY_REPO="$(cd "$HERE/.." && pwd)"
WS="$(cd "$UNITY_REPO/../../.." && pwd)"

export SSHPASS="$(grep '^VNC_PASS=' "$WS/.secrets/ard_mac.env" | cut -d= -f2-)"
. "$HERE/_host_resolve.sh"
HOST="$(resolve_build_host "${HOST:-mini}")"
RHOME="$(sshpass -e ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "developer@$HOST" 'echo $HOME')"
REMOTE="${REMOTE:-$RHOME/blueagent-unity}"
UNITY="${UNITY:-/Applications/Unity/Hub/Editor/6000.6.5f1/Unity.app/Contents/MacOS/Unity}"
APK_NAME="BlueAgentNext.apk"
# 驗收鉤子：`BA_SELFTEST=1 bash tools/build_next_android.sh` ⇒ 把 `BA_SELFTEST` 編進包裡
# （目前只有 `NextVoice` 的 wav 注入，給「模擬器沒有麥克風」時驗 /api/audio 用）。正式包不要帶。
SELFTEST="${BA_SELFTEST:-0}"

bash "$HERE/check_android_manifests.sh" || { echo "✗ manifest 不合法 ⇒ 不建置"; exit 2; }
# 2026-10-23 白屏事故：NextStrings 重複 key ⇒ 靜態建構子丟 TypeInitializationException ⇒ body 全空
python3 "$HERE/check_next_strings.py" || { echo "✗ NextStrings 不合法 ⇒ 不建置"; exit 2; }

echo "── 建置前清理：共用邏輯層的 dotnet 中間產物 ──"
# `deploy_unity.sh` 是覆蓋式同步 ⇒ Mac 上跑過 dotnet test 的 obj/bin 會留著，
# 而 `shared/BlueAgent.Logic/` 是 UPM 本地包 ⇒ 392 條 CS0436（Build result 還是 Succeeded）。
sshpass -e ssh -o ConnectTimeout=15 "developer@$HOST" \
  "rm -rf '$REMOTE'/shared/*/obj '$REMOTE'/shared/*/bin" \
  && echo "✓ 已清（shared/*/obj、shared/*/bin）" || echo "⚠️ 清理指令失敗（照舊建置）"

echo "── 建置前檢查：Mac 的可用空間（門檻 5 GiB）──"
MIN_FREE_KB=$((5 * 1024 * 1024))
FREE_KB=$(sshpass -e ssh -o ConnectTimeout=15 "developer@$HOST" "df -k /System/Volumes/Data | tail -1 | awk '{print \$4}'")
if [ -z "$FREE_KB" ] || [ "$FREE_KB" -lt "$MIN_FREE_KB" ]; then
  echo "✗ Mac 可用空間不足：${FREE_KB} KiB（門檻 5 GiB）"
  exit 3
fi
echo "✓ 可用 $(numfmt --to=iec "$(( FREE_KB * 1024 ))" 2>/dev/null || echo "${FREE_KB} KiB")"

echo "── Mac 上無頭建置（Next／Android／arm64／IL2CPP）──"
SUMMARY="$(sshpass -e ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "developer@$HOST" \
  "cd '$REMOTE/BlueAgentUnity' && BA_SELFTEST='$SELFTEST' BA_NEXT_APK_OUT='$REMOTE/Builds/$APK_NAME' '$UNITY' \
     -batchmode -quit -projectPath . \
     -executeMethod BlueAgent.EditorTools.HeadlessBuild.BuildNextAndroid \
     -logFile '$REMOTE/build-next-android.log'; \
   echo '—— log 摘要 ——'; \
   grep -E '\[next\]|error CS|Error building|Build (result|succeeded|failed)|Total build|Player build' \
     '$REMOTE/build-next-android.log' | tail -25; \
   ls -la '$REMOTE/Builds/' 2>/dev/null")"
echo "$SUMMARY"

if ! echo "$SUMMARY" | grep -q "Build result=Succeeded"; then
  echo "✗ 建置沒成功（log 裡沒有 'Build result=Succeeded'）⇒ **不拉 APK**"
  echo "  完整 log：$HOST:$REMOTE/build-next-android.log"
  exit 1
fi

echo "── 把 APK 拉回 NAS ──"
mkdir -p "$UNITY_REPO/Builds"
if ! sshpass -e scp -o ConnectTimeout=15 "developer@$HOST:$REMOTE/Builds/$APK_NAME" "$UNITY_REPO/Builds/"; then
  echo "✗ scp 失敗：APK 沒拉回 NAS ⇒ **不要往下裝機**"
  exit 1
fi

# 逐項比對「Mac 最新產物」與「NAS 這一顆」（size ＋ md5；log 的 size= 不是 APK 位元組數）
MAC_STAT=$(sshpass -e ssh -o ConnectTimeout=15 "developer@$HOST" \
  "stat -f '%z %m' '$REMOTE/Builds/$APK_NAME'; md5 -q '$REMOTE/Builds/$APK_NAME'")
MAC_SIZE=$(echo "$MAC_STAT" | sed -n 1p | awk '{print $1}')
MAC_MD5=$(echo "$MAC_STAT" | sed -n 2p)
NAS_SIZE=$(stat -c '%s' "$UNITY_REPO/Builds/$APK_NAME")
NAS_MD5=$(md5sum "$UNITY_REPO/Builds/$APK_NAME" | cut -d' ' -f1)
echo "  Mac：$MAC_SIZE bytes md5 $MAC_MD5"
echo "  NAS：$NAS_SIZE bytes md5 $NAS_MD5"
if [ "$MAC_MD5" != "$NAS_MD5" ]; then
  echo "✗ 拉回來的 APK 與 Mac 最新產物**不一致** ⇒ 先解決再裝機"
  exit 1
fi
echo "✓ NAS 上的 APK ＝ Mac 最新產物（$NAS_SIZE bytes）⇒ $UNITY_REPO/Builds/$APK_NAME"
bash "$HERE/check_phone_apk.sh" "$UNITY_REPO/Builds/$APK_NAME"
