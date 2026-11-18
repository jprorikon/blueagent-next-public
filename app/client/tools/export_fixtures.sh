#!/usr/bin/env bash
# 產生共用邏輯層的對拍真值（golden fixtures）。
#
# 真值＝**Kotlin 那一版跑出來的輸出**（`blueagent/live2d/android/kmp`）——C#／Unity 那一份
# （`blueagent-next/app/client/shared/BlueAgent.Logic`）拿它逐欄對拍。⇒ fixture 是產物，不要手改。
#
# 用法：bash blueagent-next/app/client/tools/export_fixtures.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
UNITY_REPO="$(cd "$HERE/.." && pwd)"
WS="$(cd "$UNITY_REPO/../../.." && pwd)"

export JAVA_HOME="$WS/android-toolchain/jdk"
export ANDROID_HOME="$WS/android-toolchain/sdk"
export PATH="$WS/android-toolchain/gradle/bin:$PATH"
export BA_FIXTURES_DIR="$UNITY_REPO/fixtures"

echo "── 匯出真值 ⇒ $BA_FIXTURES_DIR ──"
cd "$WS/blueagent/live2d/android"
gradle --no-daemon :app:testDebugUnitTest --tests '*FixtureExportTest*'

# ── 引擎語意層（M4）：真值＝**自錄軌跡**，不是從 fixture 讀 ──
# 為什麼是另一支模組：`:engine` 是純 JVM（引擎那半零平台依賴、不碰 android.*、不編 cpp），
# 所以它不需要 Android SDK 就編得動，跑起來也快。開發場 `workspace/live2d-native` 已由 owner
# 於 2026-10-11 移除（那 41 支對拍測試與 18 MB fixture 一起沒了）⇒ **裝置上跑的就是這一份引擎，
# 它的輸出就是真值**（見 `blueagent/HANDOFF-20261012a_unity-m4.md` §3.2／§3.3）。
# 匯出物：`$BA_FIXTURES_DIR/engine_*.json`（C# 側 `Engine*FixtureTest.cs` 逐幀重放比對）。
echo "── 匯出引擎語意層真值（`:engine` 純 JVM 測試床）──"
gradle --no-daemon :engine:test

echo "── 落地的 fixture ──"
ls -la "$BA_FIXTURES_DIR"
