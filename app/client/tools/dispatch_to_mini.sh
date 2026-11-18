#!/usr/bin/env bash
# 把一支驗收腳本 ＋ 目前 Builds 的 APK 送到 Mac mini 上跑（通用殼，省得每次打長命令）
# 用法：bash tools/_mini_dispatch.sh tools/_verify_settings_close.sh v13
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
export SSHPASS="$(grep '^VNC_PASS=' "$REPO/../../../.secrets/ard_mac.env" | cut -d= -f2-)"
MINI="${MINI:-localhost.local}"
SSHOPT="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=20"
SRC="${1:?用法: _mini_dispatch.sh <本機腳本> [TAG]}"
TAG="${2:-run}"
BASE="$(basename "$SRC")"
sshpass -e scp $SSHOPT "$SRC" "developer@$MINI:/tmp/$BASE" >/dev/null
sshpass -e scp $SSHOPT "$REPO/Builds/BlueAgentSpike.apk" "developer@$MINI:/tmp/blueagent.apk" >/dev/null
sshpass -e ssh $SSHOPT "developer@$MINI" "TAG=$TAG bash /tmp/$BASE"
