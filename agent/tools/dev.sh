#!/usr/bin/env bash
# 本機開發一鍵：測試／iOS 模擬器出包。把「該用哪支直譯器、哪個 dotnet、建置工作區在哪」固定下來
# （2026-10-31：本機 python3 是 3.9 跑不了、dotnet 不在 PATH、iOS 要先同步到建置工作區——每次都卡）。
#
#   bash agent/tools/dev.sh test            # 後端全量 + C# 邏輯層
#   bash agent/tools/dev.sh test-py [args]  # 只跑後端（args 傳給 unittest，例：test_motion test_tool_tiers）
#   bash agent/tools/dev.sh test-cs         # 只跑 C# 邏輯層
#   bash agent/tools/dev.sh ios-sim         # 同步 → Unity 出 Xcode 專案 → xcodebuild → 裝進開著的模擬器 → 啟動
#
# 可覆寫：PYTHON、DOTNET、UNITY、BUILD_WS（Unity 建置工作區；外置盤 /Volumes/external 掛著時預設放那裡，否則 ~/blueagent-unity）、SIM（模擬器，預設 booted）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"   # 倉庫根
UNITY_VER="6000.6.5f1"
UNITY="${UNITY:-/Applications/Unity/Hub/Editor/$UNITY_VER/Unity.app/Contents/MacOS/Unity}"
# 建置工作區（Unity Library＋Xcode 產物，動輒十幾 GB）：外置盤掛著就放外置盤，系統盤不夠放（2026-11-08 塞滿過一次）
EXT_WS="/tmp/blueagent-next-build/build-cache/dev-unity"
if [ -z "${BUILD_WS:-}" ] && [ -d /tmp/blueagent-next-build ]; then BUILD_WS="$EXT_WS"; fi
BUILD_WS="${BUILD_WS:-$HOME/blueagent-unity}"
SIM="${SIM:-booted}"
BUNDLE="dev.deepsea.blueagent.next"

pick_python() {
  if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return; fi
  for cand in python3.12 python3.11 python3.10 /opt/homebrew/bin/python3.12 python3; do
    if command -v "$cand" >/dev/null 2>&1 \
       && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      echo "$cand"; return
    fi
  done
  echo "✗ 找不到 Python ≥ 3.10（image 是 3.11；dataclass slots= 需要 3.10+）" >&2; exit 2
}

pick_dotnet() {
  if [ -n "${DOTNET:-}" ]; then echo "$DOTNET"; return; fi
  for cand in dotnet "$HOME/.dotnet/dotnet" \
      "/Applications/Unity/Hub/Editor/$UNITY_VER/Unity.app/Contents/Resources/Scripting/DotNetSdk/dotnet"; do
    if command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; then echo "$cand"; return; fi
  done
  echo "✗ 找不到 dotnet（Unity 內建的 .NET SDK 也沒有）" >&2; exit 2
}

test_py() {
  local py; py="$(pick_python)"
  cd "$ROOT/agent/tests"
  if [ "$#" -gt 0 ]; then
    BLUEAGENT_BRAIN=mock PYTHONPATH="$ROOT:$ROOT/agent/tests" "$py" -m unittest "$@"
  else
    "$py" "$ROOT/agent/tests/run_all.py" 2>&1 | grep -E "^(ERROR|FAIL):|^Ran |^OK|^FAILED" || true
  fi
}

test_cs() {
  local dn; dn="$(pick_dotnet)"
  cd "$ROOT/app/client/shared/BlueAgent.Logic.Tests"
  DOTNET_CLI_TELEMETRY_OPTOUT=1 "$dn" test --nologo 2>&1 | grep -E "error CS|失敗|Failed|合格|Passed!" | tail -5
}

ios_sim() {
  local repo="$ROOT/app/client"
  echo "── 同步到建置工作區 $BUILD_WS ──"
  rsync -a --delete --exclude '*.meta' "$repo/BlueAgentUnity/Assets/Scripts/" "$BUILD_WS/BlueAgentUnity/Assets/Scripts/"
  rsync -a --delete --exclude '*.meta' "$repo/BlueAgentUnity/Assets/Editor/" "$BUILD_WS/BlueAgentUnity/Assets/Editor/"
  rsync -a --exclude '*.meta' "$repo/BlueAgentUnity/Assets/Resources/" "$BUILD_WS/BlueAgentUnity/Assets/Resources/"
  rsync -a --exclude '*.meta' "$repo/BlueAgentUnity/Assets/Shaders/" "$BUILD_WS/BlueAgentUnity/Assets/Shaders/" 2>/dev/null || true
  rsync -a --exclude '*.meta' "$repo/BlueAgentUnity/Assets/Plugins/iOS/" "$BUILD_WS/BlueAgentUnity/Assets/Plugins/iOS/"
  rsync -a --delete --exclude '*.meta' --exclude bin --exclude obj \
    "$repo/shared/BlueAgent.Logic/" "$BUILD_WS/shared/BlueAgent.Logic/"
  # 套件清單也要跟（2026-11-01：MR 端帶進 AR Foundation／Input System／XR Hands，工作區還是舊清單 ⇒ MR 腳本編不過、整包出不來）
  cp "$repo/BlueAgentUnity/Packages/manifest.json" "$repo/BlueAgentUnity/Packages/packages-lock.json" "$BUILD_WS/BlueAgentUnity/Packages/"
  # 刪掉的 .cs 留下的孤兒 .meta（Unity 會警告）
  find "$BUILD_WS/BlueAgentUnity/Assets/Scripts" "$BUILD_WS/BlueAgentUnity/Assets/Editor" "$BUILD_WS/shared/BlueAgent.Logic" \
    -name "*.cs.meta" | while read -r meta; do [ -f "${meta%.meta}" ] || rm -f "$meta"; done

  echo "── Unity 出 Xcode 專案（模擬器切片）──"
  ( cd "$BUILD_WS/BlueAgentUnity" && BA_IOS_SIM=1 "$UNITY" -batchmode -quit -projectPath . -buildTarget iOS \
      -executeMethod BlueAgent.EditorTools.HeadlessBuild.BuildNextIOS -logFile "$BUILD_WS/build-next-ios-sim.log" ) || true
  grep -E "error CS|Build result" "$BUILD_WS/build-next-ios-sim.log" | sort -u | tail -8
  grep -q "Build result=Succeeded" "$BUILD_WS/build-next-ios-sim.log" || { echo "✗ Unity 出包失敗（log：$BUILD_WS/build-next-ios-sim.log）"; exit 1; }

  echo "── xcodebuild ──"
  local dd="$BUILD_WS/Builds/next-ios-sim-dd"
  ( cd "$BUILD_WS/BlueAgentUnity/Builds/next-ios" && xcodebuild -project Unity-iPhone.xcodeproj -scheme Unity-iPhone \
      -configuration Release -sdk iphonesimulator -arch arm64 -derivedDataPath "$dd" \
      CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO build > "$BUILD_WS/xcodebuild-next-sim.log" 2>&1 ) \
    || { grep -E " error" "$BUILD_WS/xcodebuild-next-sim.log" | head; exit 1; }
  local app="$dd/Build/Products/Release-iphonesimulator/BlueAgentNext.app"
  echo "✓ $(du -sh "$app" | cut -f1) $app"

  echo "── 裝進模擬器並啟動（登入資料保留）──"
  xcrun simctl install "$SIM" "$app"
  xcrun simctl launch --terminate-running-process "$SIM" "$BUNDLE"
}

case "${1:-}" in
  test)    shift; test_py; test_cs ;;
  test-py) shift; test_py "$@" ;;
  test-cs) shift; test_cs ;;
  ios-sim) shift; ios_sim ;;
  *) sed -n '2,12p' "$0"; exit 1 ;;
esac
