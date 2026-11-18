#!/usr/bin/env bash
# 【在 Mac mini 上跑】装 APK → 连拍开机 → 进设定换背景 → 抓 log。
# 环境变量：TAG（截图前缀）、AVD（AVD 名）。
# ⚠️ 由 NAS 侧 scp 过来再用 `ssh 'TAG=… bash /tmp/emu_remote.sh'` 跑（不要用 heredoc 拼变量）。
set -uo pipefail
export PATH="$HOME/Android/Sdk/platform-tools:$HOME/Android/Sdk/emulator:$PATH"
APP=dev.deepsea.blueagent.unity
TAG="${TAG:-emu}"
AVD="${AVD:-blueagent}"
APK="${APK:-/tmp/blueagent.apk}"

if ! pgrep -f "emulator -avd $AVD" >/dev/null; then
  nohup emulator -avd "$AVD" -no-window -no-audio -no-boot-anim -no-snapshot \
        -gpu swiftshader_indirect > /tmp/emu.log 2>&1 &
  echo "  起了模拟器（日志 /tmp/emu.log）"
else
  echo "  模拟器已在跑"
fi
adb wait-for-device
for i in $(seq 1 60); do
  [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && { echo "  开机完成"; break; }
  sleep 5
done
SZ=$(adb shell wm size | sed 's/.*: //' | tr -d '\r')
W=${SZ%x*}; H=${SZ#*x}
echo "  解析度=$SZ"

# ⚠️ 这个模拟器上 `input tap` **完全无效**（手机上是有效的；实测三轮点击全被吞）
#    ⇒ 改用手机上验证过的 `input motionevent DOWN/UP`（ShellRouter 的点击路径吃得到）。
tap() {
  adb shell input motionevent DOWN $((W*$1/100)) $((H*$2/100))
  sleep 0.08
  adb shell input motionevent UP $((W*$1/100)) $((H*$2/100))
}
shot() { adb shell screencap -p /sdcard/s.png >/dev/null 2>&1; adb pull /sdcard/s.png "/tmp/$TAG-$1.png" >/dev/null 2>&1; }

echo "  装 APK：$(adb install -r "$APK" 2>&1 | tail -1)"
adb shell am force-stop "$APP"
# ⚠️ 全新安装第一次启动会弹两个**系统**窗口，会把后面所有点击都吃掉（2026-10-17 实测：
#    连续三輪「点了没反应」，其实全打在弹窗上）：① 麦克风权限弹窗；② 全屏提示卡「Viewing full screen」。
#    ⇒ 先预授权限（弹窗根本不出现）＋ 把全屏提示标记成已确认。
adb shell pm grant "$APP" android.permission.RECORD_AUDIO 2>/dev/null || true
adb shell settings put secure immersive_mode_confirmations confirmed 2>/dev/null || true
adb shell logcat -c
# ⚠️ **不要硬写入口类名**（2026-10-17 实测：硬写的 `UnityPlayerGameActivity` 是幽灵 activity
#    ⇒ `ClassNotFoundException` 直接崩）。改用系统解析出来的 LAUNCHER 组件——顺便也验证
#    「桌面图标走的那条路」是通的。
ENTRY=$(adb shell cmd package resolve-activity --brief "$APP" 2>/dev/null | tail -1 | tr -d '\r')
echo "  入口（系统解析）=$ENTRY"
adb shell am start -n "$ENTRY" >/dev/null 2>&1

echo "  ── ① 开机连拍 30 张（每 0.3s）──"
i=0
while [ "$i" -lt 30 ]; do shot "$(printf 'boot%02d' "$i")"; i=$((i+1)); sleep 0.3; done
for f in /tmp/"$TAG"-boot*.png; do printf "%s:%s " "$(basename "$f" .png)" "$(stat -f '%z' "$f")"; done
echo

echo "  ── ② 进 设定→背景 ──"
tap 6 3;   sleep 3
tap 72 56; sleep 4
tap 13 32; sleep 4
shot "panel"
adb shell logcat -d | grep -E "\[ui\] 疊層|設定分頁" | tail -3

echo "  ── ③ 换背景：点面板中段那张卡 ──"
adb shell logcat -c
tap 60 50; sleep 4
shot "panel-after"
tap 95 11; sleep 4          # ✕ 关掉设定 → 回主介面
shot "home-after"
echo "  这一击的 log："
adb shell logcat -d | grep -E "\[ui\] 背景|\[ui\] 點到 Card|\[ui\] 卡片|\[stage-bg\]" | tail -6
echo "  舞台背景 log："
adb shell logcat -d | grep -E "\[stage-bg\]" | tail -4
echo "  连拍与其它截图（/tmp/$TAG-*.png）："
ls -la /tmp/"$TAG"-*.png | tail -6
