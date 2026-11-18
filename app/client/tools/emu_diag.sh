#!/usr/bin/env bash
# 【在 Mac mini 上跑】诊断：APK 装得上，但 app 起不来（停在 launcher）。
# 抓：am start 的返回、activity 栈、以及有没有崩溃栈。
set -uo pipefail
export PATH="$HOME/Android/Sdk/platform-tools:$HOME/Android/Sdk/emulator:$PATH"
APP=dev.deepsea.blueagent.unity
ACT="$APP/com.unity3d.player.UnityPlayerGameActivity"
AVD="${AVD:-blueagent}"

if ! pgrep -f "emulator -avd $AVD" >/dev/null; then
  nohup emulator -avd "$AVD" -no-window -no-audio -no-boot-anim -no-snapshot \
        -gpu swiftshader_indirect > /tmp/emu.log 2>&1 &
  echo "  起了模拟器"
fi
adb wait-for-device
for i in $(seq 1 60); do
  [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && break
  sleep 5
done
echo "  开机完成（abi=$(adb shell getprop ro.product.cpu.abi | tr -d '\r')）"

echo "── 装 ──"
adb install -r /tmp/blueagent.apk 2>&1 | tail -2

echo "── 这个包里有哪些可启动的 activity ──"
adb shell cmd package resolve-activity --brief "$APP" 2>&1 | tail -3
adb shell dumpsys package "$APP" | grep -A3 "android.intent.action.MAIN" | head -8

echo "── 清 log 后启动 ──"
adb shell am force-stop "$APP"
adb shell logcat -c
echo "am start 回："
adb shell am start -W -n "$ACT" 2>&1 | tail -6
sleep 8
echo "── 现在前台是谁 ──"
adb shell dumpsys activity activities 2>/dev/null | grep -E "mResumedActivity|topResumedActivity|blueagent" | head -6
echo "── 有没有崩溃 ──"
adb shell logcat -d 2>/dev/null | grep -E "FATAL|AndroidRuntime|beginning of crash|Unity|blueagent" | tail -20
echo "── 截图 ──"
adb shell screencap -p /sdcard/d.png >/dev/null 2>&1
adb pull /sdcard/d.png /tmp/diag.png >/dev/null 2>&1
stat -f "  diag.png=%z bytes" /tmp/diag.png
