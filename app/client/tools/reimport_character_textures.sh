#!/usr/bin/env bash
# 逼 Mac 上的 Unity **重新匯入立繪貼圖**（`.meta` 砍掉 ＋ 摸一下 PNG）。
#
# 為什麼需要：`CharacterTexturePostprocessor` 只在**重新匯入**時生效，而 Mac 上的
# `Assets/Resources/characters/**` 早就用舊設定（NPOT=ToNearest ⇒ 768x765 被縮成 1024x512）
# 匯入過了，`.meta` 與 Library 快取都留著 ⇒ 不砍不會重跑。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
UNITY_REPO="$(cd "$HERE/.." && pwd)"
WS="$(cd "$UNITY_REPO/../../.." && pwd)"

export SSHPASS="$(grep '^VNC_PASS=' "$WS/.secrets/ard_mac.env" | cut -d= -f2-)"
# 建置機：預設 mini、Air 備用（HOST=air 切回 Air；也可直接給 IP／主機名）
. "$(dirname "$0")/_host_resolve.sh"
HOST="$(resolve_build_host "${HOST:-mini}")"
RHOME="$(sshpass -e ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "developer@$HOST" 'echo $HOME')"
REMOTE="${REMOTE:-$RHOME/blueagent-unity}"
DIR="$REMOTE/BlueAgentUnity/Assets/Resources/characters"

echo "── 砍掉立繪貼圖的 .meta（讓 Unity 用新的匯入設定重跑）──"
sshpass -e ssh -o ConnectTimeout=15 "developer@$HOST" \
  "cd '$DIR' && find . -name '*.png.meta' -delete && find . -name '*.png' -exec touch {} + && find . -name '*.png' | wc -l"
echo "✓ 重匯入已排好（下次 Unity 開啟／建置時生效）"
