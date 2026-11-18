#!/usr/bin/env bash
# 【在 Mac mini 上跑】问清楚：这个模拟器上哪种触摸注入有效（tap / motionevent / swipe / -d 0）。
# 判据：点左上 ☰ 后 logcat 出现 `[ui] 疊層：menu`（app 自己的 log）。
set -uo pipefail
export PATH="$HOME/Android/Sdk/platform-tools:$HOME/Android/Sdk/emulator:$PATH"
APP=dev.deepsea.blueagent.unity
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
echo "  android=$(adb shell getprop ro.build.version.release | tr -d '\r')（API $(adb shell getprop ro.build.version.sdk | tr -d '\r')）"
echo "  abi=$(adb shell getprop ro.product.cpu.abi | tr -d '\r')"
adb shell settings put secure immersive_mode_confirmations confirmed 2>/dev/null || true
adb shell pm grant "$APP" android.permission.RECORD_AUDIO 2>/dev/null || true
adb install -r /tmp/blueagent.apk 2>&1 | tail -1
SZ=$(adb shell wm size | sed 's/.*: //' | tr -d '\r'); W=${SZ%x*}; H=${SZ#*x}
X=$((W*6/100)); Y=$((H*3/100))
echo "  解析度=$SZ ⇒ ☰ 在 ($X,$Y)"
echo "  displays: $(adb shell dumpsys SurfaceFlinger --display-id 2>/dev/null | head -2 | tr '\n' ' ')"

try() {   # $1=方法名，后面是命令
  local name="$1"; shift
  adb shell am force-stop "$APP" >/dev/null 2>&1
  adb shell logcat -c
  ENTRY=$(adb shell cmd package resolve-activity --brief "$APP" 2>/dev/null | tail -1 | tr -d '\r')
  adb shell am start -n "$ENTRY" >/dev/null 2>&1
  sleep 9
  "$@" >/dev/null 2>&1
  sleep 3
  if adb shell logcat -d 2>/dev/null | grep -q "疊層：menu"; then
    echo "  ✓ $name ⇒ 有效"
    return 0
  fi
  echo "  ✗ $name ⇒ 无效"
  return 1
}

try "input tap"          adb shell input tap "$X" "$Y" && exit 0
try "input -d 0 tap"     adb shell input -d 0 tap "$X" "$Y" && exit 0
try "input touchscreen tap" adb shell input touchscreen tap "$X" "$Y" && exit 0
try "input swipe(短)"     adb shell input swipe "$X" "$Y" "$X" "$Y" 120 && exit 0
try "motionevent DOWN/UP" bash -c "adb shell input motionevent DOWN $X $Y; sleep 0.1; adb shell input motionevent UP $X $Y" && exit 0
echo "  ⇒ 五种都不行：这台的注入通道有别的限制（改用真机验收）"
