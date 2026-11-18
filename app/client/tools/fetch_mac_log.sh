#!/usr/bin/env bash
# 把 Mac 建置工作區的某支 log 拉回本地看（無頭 Unity 的 log 在遠端，直接 grep 會遇到引號地獄）。
#
# 用法：bash blueagent-next/app/client/tools/fetch_mac_log.sh screen.log [grep 樣式]
set -euo pipefail

LOGNAME="${1:-screen.log}"
PATTERN="${2:-}"

HERE="$(cd "$(dirname "$0")" && pwd)"
UNITY_REPO="$(cd "$HERE/.." && pwd)"
WS="$(cd "$UNITY_REPO/../../.." && pwd)"
export SSHPASS="$(grep '^VNC_PASS=' "$WS/.secrets/ard_mac.env" | cut -d= -f2-)"
# 建置機：預設 mini、Air 備用（HOST=air 切回 Air；也可直接給 IP／主機名）
. "$(dirname "$0")/_host_resolve.sh"
HOST="$(resolve_build_host "${HOST:-mini}")"
RHOME="$(sshpass -e ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "developer@$HOST" 'echo $HOME')"
REMOTE="${REMOTE:-$RHOME/blueagent-unity}"

mkdir -p "$UNITY_REPO/Builds"
OUT="$UNITY_REPO/Builds/mac-$LOGNAME"
sshpass -e scp -o ConnectTimeout=15 "developer@$HOST:$REMOTE/$LOGNAME" "$OUT"
echo "⇒ $OUT（$(wc -l < "$OUT") 行）"

if [ -n "$PATTERN" ]; then
  echo "── 命中「$PATTERN」的行 ──"
  grep -nE "$PATTERN" "$OUT" | head -60 || echo "（沒有命中）"
fi
