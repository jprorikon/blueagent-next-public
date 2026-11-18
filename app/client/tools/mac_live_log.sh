#!/usr/bin/env bash
# 看 Mac 上那一輪在線探針的關鍵行（模型組件／嘴／總結）。用法：bash tools/mac_live_log.sh
set -uo pipefail
WS="$(cd "$(dirname "$0")/../../../.." && pwd)"
export SSHPASS="$(grep '^VNC_PASS=' "$WS/.secrets/ard_mac.env" | cut -d= -f2-)"
sshpass -e ssh -o ConnectTimeout=15 developer@100.64.0.1 'grep -E "\[live\]|\[mouth\]|\[bridge\]|\[engine\] 接線|\[http\]|讓位" /tmp/ba-live.log | grep -v "取景" | head -45'
