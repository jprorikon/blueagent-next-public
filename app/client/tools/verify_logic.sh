#!/usr/bin/env bash
# 共用邏輯層的**驗收迴圈**：Kotlin 側匯出真值 → C# 側逐條對拍。
#
# 為什麼要有這一支：這條迴圈一輪至少跑三次（每次動到共用邏輯層），而且順序不能顛倒
# ——**fixture 沒更新就跑測試＝拿舊真值驗新碼**（會給出假的綠）。一條命令＝沒有手滑的空間。
#
# 用法：
#   bash blueagent-next/app/client/tools/verify_logic.sh            # 匯出真值 ＋ 跑全部測試
#   bash blueagent-next/app/client/tools/verify_logic.sh --no-export  # 只跑測試（真值沒動時）
#   bash blueagent-next/app/client/tools/verify_logic.sh --filter JsonFixtureTest   # 只跑某一支
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
UNITY_REPO="$(cd "$HERE/.." && pwd)"
WS="$(cd "$UNITY_REPO/../../.." && pwd)"
export PATH="$HOME/.dotnet:$PATH"

DO_EXPORT=1
FILTER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-export) DO_EXPORT=0 ;;
    --filter) shift; FILTER="$1" ;;
    *) echo "未知參數：$1" >&2; exit 2 ;;
  esac
  shift
done

if [ "$DO_EXPORT" = "1" ]; then
  bash "$HERE/export_fixtures.sh"
fi

echo "── 產生物是否為最新（心情原型表；機器翻的，不要手改）──"
python3 "$HERE/gen_emotion_archetypes_cs.py" --check
# 語音表演表（gen_speech_tables_cs.py）隨舊殼的表演引擎一起移除了（2026-10-31）

echo "── C# 側逐條對拍 ──"
cd "$UNITY_REPO"
if [ -n "$FILTER" ]; then
  dotnet test shared/BlueAgent.Logic.Tests/BlueAgent.Logic.Tests.csproj --nologo \
    --logger "console;verbosity=detailed" --filter "FullyQualifiedName~$FILTER" 2>&1 \
    | grep -E "Passed!|Failed!|error CS|條|case|全數相同" | sort -u
else
  dotnet test shared/BlueAgent.Logic.Tests/BlueAgent.Logic.Tests.csproj --nologo \
    --logger "console;verbosity=detailed" 2>&1 \
    | grep -E "Passed!|Failed!|error CS|條 case|與 Kotlin|條目|全數相同" | sort -u
fi

echo "── 判準提醒 ──"
echo "  * 「fixture 是產物」：要加 case 改 Kotlin 那支匯出器，不要手改 JSON。"
echo "  * 剛動過共用邏輯層就要**跑完整**（只跑一支會漏掉被牽動的別人）。"
echo "  * 別人（含子代理）交回來的移植碼**未經編譯**——這裡才是判準。"
echo "  * 動過引擎（blueagent/live2d/android/live2d/.../engine）⇒ 真值要**重錄**"
echo "    （export_fixtures.sh 裡的 \`:engine:test\` 就是那支錄音機）。"
