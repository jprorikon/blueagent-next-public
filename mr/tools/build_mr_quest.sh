#!/usr/bin/env bash
# 無頭建 Quest 3 MR 包（dev.deepsea.blueagent.mr；Android／arm64／IL2CPP／OpenXR）＋ manifest 閘門。
# /mr 只持有 Quest 專有內容；sync_workspace.py 會把 app/client 的共享執行期組合進構建工作區。
#
# 用法：
#   bash mr/tools/build_mr_quest.sh            # 預設 mini；HOST=air 換建置機
#   LOCAL=1 bash mr/tools/build_mr_quest.sh    # 本機 Unity 6000.6.5f1＋Android 模組
#   BA_MR_BUILD_ROOT=/Volumes/<碟>/blueagent-mr LOCAL=1 bash mr/tools/build_mr_quest.sh
# 產物：mr/Builds/BlueAgentMR.apk；侧載：adb install -r mr/Builds/BlueAgentMR.apk
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
MR_REPO="$(cd "$HERE/.." && pwd)"
WS="$(cd "$MR_REPO/.." && pwd)"
UNITY="${UNITY:-/Applications/Unity/Hub/Editor/6000.6.5f1/Unity.app/Contents/MacOS/Unity}"
APK_NAME="BlueAgentMR.apk"
BUILD_ROOT="${BA_MR_BUILD_ROOT:-$MR_REPO/.build}"
WORKSPACE="$BUILD_ROOT/workspace"
LOG="$BUILD_ROOT/build-mr-quest.log"

python3 "$WS/app/client/tools/check_next_strings.py" || { echo "✗ NextStrings 不合法 ⇒ 不建置"; exit 2; }
python3 "$HERE/sync_workspace.py" --workspace "$WORKSPACE"
bash "$WS/app/client/tools/check_android_manifests.sh" "$WORKSPACE/mr/BlueAgentUnity" \
  || { echo "✗ manifest 不合法 ⇒ 不建置"; exit 2; }
mkdir -p "$MR_REPO/Builds" "$BUILD_ROOT"

if [ "${LOCAL:-0}" = 1 ]; then
  # Unity／Bee 在本機 headless 環境無法自動推斷 Stevedore cache；跟工程一起放在指定構建根。
  export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$BUILD_ROOT/cache}"
  export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$BUILD_ROOT/config}"
  echo "── 本機無頭建置（MR／Quest 3）──"
  ( cd "$WORKSPACE/mr/BlueAgentUnity" && BA_MR_APK_OUT="$MR_REPO/Builds/$APK_NAME" "$UNITY" \
      -batchmode -quit -projectPath . -executeMethod BlueAgent.EditorTools.MrBuild.BuildMrQuest -logFile "$LOG" ) || true
  grep -E '\[mr\]|error CS|Build Finished' "$LOG" | tail -20 || true
  grep -q "\[mr\] Build result=Succeeded" "$LOG" || { echo "✗ 建置沒成功（完整 log：$LOG）"; exit 1; }
else
  export SSHPASS="$(grep '^VNC_PASS=' "$WS/.secrets/ard_mac.env" | cut -d= -f2-)"
  . "$WS/app/client/tools/_host_resolve.sh"
  HOST="$(resolve_build_host "${HOST:-mini}")"
  RHOME="$(sshpass -e ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "developer@$HOST" 'echo $HOME')"
  REMOTE="${REMOTE:-$RHOME/blueagent-mr}"
  ROOTS=(mr/BlueAgentUnity/Assets mr/BlueAgentUnity/Packages mr/BlueAgentUnity/ProjectSettings mr/BlueAgentUnity/Keys app/client/shared app/client/fixtures)
  echo "── 同步組合原始碼 ⇒ developer@$HOST:$REMOTE ──"
  sshpass -e ssh -o ConnectTimeout=15 "developer@$HOST" \
    "rm -rf '$REMOTE/mr/BlueAgentUnity/Assets' '$REMOTE/mr/BlueAgentUnity/Packages' '$REMOTE/mr/BlueAgentUnity/ProjectSettings' '$REMOTE/mr/BlueAgentUnity/Keys' '$REMOTE/app/client/shared' '$REMOTE/app/client/fixtures'; mkdir -p '$REMOTE'"
  tar czf - -C "$WORKSPACE" "${ROOTS[@]}" | sshpass -e ssh -o ConnectTimeout=30 "developer@$HOST" "tar xzf - -C '$REMOTE'"

  echo "── 建置機無頭建置（MR／Quest 3）@ $HOST ──"
  SUMMARY="$(sshpass -e ssh -o ConnectTimeout=15 "developer@$HOST" \
    "mkdir -p '$REMOTE/mr/Builds'; cd '$REMOTE/mr/BlueAgentUnity' && BA_MR_APK_OUT='$REMOTE/mr/Builds/$APK_NAME' '$UNITY' \
       -batchmode -quit -projectPath . -executeMethod BlueAgent.EditorTools.MrBuild.BuildMrQuest \
       -logFile '$REMOTE/build-mr-quest.log'; \
     grep -E '\[mr\]|error CS|Build Finished' '$REMOTE/build-mr-quest.log' | tail -20")" || true
  echo "$SUMMARY"
  echo "$SUMMARY" | grep -q "\[mr\] Build result=Succeeded" \
    || { echo "✗ 建置沒成功 ⇒ 不拉 APK（log：$HOST:$REMOTE/build-mr-quest.log）"; exit 1; }
  sshpass -e scp -o ConnectTimeout=15 "developer@$HOST:$REMOTE/mr/Builds/$APK_NAME" "$MR_REPO/Builds/" \
    || { echo "✗ scp 失敗：APK 沒拉回來"; exit 1; }
fi

bash "$HERE/check_mr_apk.sh" "$MR_REPO/Builds/$APK_NAME"
echo "✓ $MR_REPO/Builds/$APK_NAME"
