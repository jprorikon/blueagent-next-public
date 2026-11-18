#!/usr/bin/env bash
# 清掉 Mac 上殘留的 Unity 進程（無頭跑批會越積越多，互搶資源 ⇒ 越跑越慢、SSH 也被拖住），
# 並把最近一次 screen.log 的關鍵行拉出來看。
#
# 為什麼要這一支：`-batchmode` 的 Unity 即使 `EditorApplication.Exit` 回報成功，
# helper（UnityHelper／bee／shader compiler）也可能留著；跑幾輪之後同時有十幾個在跑，
# 這一輪就是這樣被拖到 17 分鐘還沒完。
#
# 用法：bash blueagent-next/app/client/tools/mac_unity_reset.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WS="$(cd "$HERE/../../../.." && pwd)"
export SSHPASS="$(grep '^VNC_PASS=' "$WS/.secrets/ard_mac.env" | cut -d= -f2-)"
# 建置機：預設 mini、Air 備用（HOST=air 切回 Air；也可直接給 IP／主機名）
. "$(dirname "$0")/_host_resolve.sh"
HOST="$(resolve_build_host "${HOST:-mini}")"

echo "── 清 Unity 進程 ──"
timeout 90 sshpass -e ssh -o ConnectTimeout=10 "developer@$HOST" \
  'pkill -f "Unity.app/Contents/MacOS/Unity" ; pkill -f UnityHelper ; sleep 2 ; echo "剩下：$(ps aux | grep -c "[U]nity")"' 2>&1 | tail -3

echo "── screen.log 關鍵行 ──"
timeout 90 sshpass -e ssh -o ConnectTimeout=10 "developer@$HOST" \
  'grep -nE "\[screen\]|Exception|error CS|NullReference" ~/blueagent-unity/screen.log | tail -12 ; echo "--- 尾 6 行 ---" ; tail -6 ~/blueagent-unity/screen.log' 2>&1 | tail -22
