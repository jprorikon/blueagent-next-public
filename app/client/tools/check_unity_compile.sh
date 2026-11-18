#!/usr/bin/env bash
# **編譯閘門**（先編一次、不畫圖）：改完 C# 要跑 6 屏 A/B 之前先過這一關。
#
# 為什麼要一支：`ab_case.sh` 的「一個 case 一屏圖」一輪要十幾分鐘，而**編譯錯誤**會在**每一屏**
# 都重跑一次才發現（2026-10-11 第三輪就白跑了兩輪、6 屏 × 2＝12 次）。Unity 在 `-batchmode -quit`
# 下建置一次 script assembly 只要一兩分鐘，而且**不進播放模式、不畫圖**。
#
# 用法：bash tools/check_unity_compile.sh        # exit 0＝沒有 `error CS`
# 產物：Mac 上的 /tmp/ba-compile.log（有錯時把錯誤行印回來）
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
UNITY_REPO="$(cd "$HERE/.." && pwd)"
WS="$(cd "$UNITY_REPO/../../.." && pwd)"

export SSHPASS="$(grep '^VNC_PASS=' "$WS/.secrets/ard_mac.env" | cut -d= -f2-)"
# 建置機：預設 mini、Air 備用（HOST=air 切回 Air；也可直接給 IP／主機名）
. "$(dirname "$0")/_host_resolve.sh"
HOST="$(resolve_build_host "${HOST:-mini}")"
UNITY="${UNITY:-/Applications/Unity/Hub/Editor/6000.6.5f1/Unity.app/Contents/MacOS/Unity}"
RHOME="$(sshpass -e ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "developer@$HOST" 'echo $HOME')"
REMOTE="${REMOTE:-$RHOME/blueagent-unity}"

echo "── 同步原始碼 ＋ 編譯閘門（不畫圖）──"
# ⚠️ manifest 先在本機驗 XML：不合法時 Unity 會在 AndroidManifestUtils.Parse 拋異常、
#    整包建置失敗卻**沒有一行 error CS**（2026-10-18 因此誤判成「Mac 髒快取」白清一次 Library）。
bash "$HERE/check_android_manifests.sh" || { echo "manifest 不合法 ⇒ 不編、不推"; exit 2; }
bash "$HERE/deploy_unity.sh" >/dev/null || { echo "同步失敗"; exit 2; }
sshpass -e ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "developer@$HOST" \
  "cd '$REMOTE/BlueAgentUnity' && '$UNITY' -batchmode -quit -projectPath . -logFile /tmp/ba-compile.log" \
  >/dev/null 2>&1
ERRORS="$(sshpass -e ssh -o ConnectTimeout=15 "developer@$HOST" \
  "grep -E 'error CS' /tmp/ba-compile.log | sort -u | head -20")"
if [ -n "$ERRORS" ]; then
  echo "✗ 編譯不過："
  echo "$ERRORS"
  exit 1
fi
echo "✓ 編譯乾淨（log：Mac 上的 /tmp/ba-compile.log）"
