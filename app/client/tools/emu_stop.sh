#!/usr/bin/env bash
# 【在 Mac mini 上跑】收起现场：停 app、停模拟器、清掉 adb 转发（技能 §6c：验完必收）。
set -uo pipefail
export PATH="$HOME/Android/Sdk/platform-tools:$PATH"
AVD="${AVD:-blueagent}"
adb shell am force-stop dev.deepsea.blueagent.unity 2>/dev/null || true
pkill -f "emulator -avd $AVD" 2>/dev/null || true
adb forward --remove-all 2>/dev/null || true
sleep 2
echo "  ✓ 已停（app / emulator / adb forward）"
pgrep -fl "emulator -avd" || echo "  （确认：没有模拟器残留在跑）"
