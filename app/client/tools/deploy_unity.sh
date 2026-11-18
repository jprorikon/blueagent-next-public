#!/usr/bin/env bash
# 把 Unity 專案（**原始碼**）從 NAS 推去建置機的建置工作區（預設 `~/blueagent-unity`），
# 並把「倉庫已刪、建置機還在」的孤兒檔清掉——即**鏡像同步**（單向：NAS → Mac）。
#
# 為什麼是這個方向：NAS 是真源（版控在這裡），Mac 只是建置工作區——與現役 `blueagent-android`／
# `blueagent-ios` 兩條線同一個模式（那兩條也是 NAS→Mac 同步後才編）。
# 只推原始碼（Assets／Packages／ProjectSettings ＋ shared 那包共用邏輯層 ＋ fixtures 那份對拍真值）；
# `Library/`（Unity 的匯入快取，數百 MB）在 Mac 上自己編，不進版控也不過網路。
#
# 2026-10-31 owner 拍板：**改成鏡像同步**（以前只 tar 覆蓋、不刪檔，兩次付出代價）：
#   · 2026-10-16：作廢的自訂 AndroidManifest 從倉庫刪了、Mac 上還留著 ⇒ 它取代 Unity 產的 launcher，
#     裝上去 `am start` 說 `Activity … does not exist`（入口 activity 整個不見）。
#   · 2026-10-31：`UiBuilder.StickerMarkup` 從倉庫移除，Mac 上三支 10-18 的舊殼畫面還在
#     ⇒ 編譯閘門 `error CS0117`，看起來像程式碼壞了，其實是工作區有孤兒。
# 刪除半邊在 `mirror_cleanup_remote.sh`（在建置機上跑）：只刪「Mac 有、這次清單沒有」的檔，
# `.meta` 只在「本體也消失」時才連著刪，`Assets/Scenes/Next.unity`（HeadlessBuild 現場生）保留。
#
# 用法：bash blueagent-next/app/client/tools/deploy_unity.sh
#   HOST=air          換備用建置機
#   REMOTE=<路徑>     換建置工作區
#   MIRROR_DRY=1      只印會刪什麼，不動手（排查用）
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
UNITY_REPO="$(cd "$HERE/.." && pwd)"
WS="$(cd "$UNITY_REPO/../../.." && pwd)"

export SSHPASS="$(grep '^VNC_PASS=' "$WS/.secrets/ard_mac.env" | cut -d= -f2-)"
# 建置機：預設 mini、Air 備用（HOST=air 切回 Air；也可直接給 IP／主機名）
. "$(dirname "$0")/_host_resolve.sh"
HOST="$(resolve_build_host "${HOST:-mini}")"
# ⚠️ `$HOME` 是 **NAS** 的家目錄，不是 Mac 的 ⇒ 遠端家目錄要問那一台（踩過：路徑變成 /vol1/… 被拒）。
RHOME="$(sshpass -e ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "developer@$HOST" 'echo $HOME')"
REMOTE="${REMOTE:-$RHOME/blueagent-unity}"

# 同步根目錄（tar 與清單共用同一份，兩邊不會走鐘）
ROOTS=(shared BlueAgentUnity/Assets BlueAgentUnity/Packages BlueAgentUnity/ProjectSettings BlueAgentUnity/Keys fixtures)
# ⚠️ `fixtures` 一定要在清單裡（2026-10-28 補）：`shared/BlueAgent.Logic.Tests.csproj` 的
#    `$(FixturesDir)` 預設＝`shared/BlueAgent.Logic.Tests/../../fixtures`（＝ `app/client/fixtures`），
#    那是**對拍真值**（Kotlin 那一版跑出來的輸出）。少了它 ⇒ Mac 上沒有 `~/blueagent-app/client/fixtures`
#    ⇒ `dotnet test` 127 條全報「找不到 fixture」＝假紅（測試本身沒壞）。

LIST="$(mktemp "${TMPDIR:-/tmp}/ba-unity-files.XXXXXX")"
trap 'rm -f "$LIST"' EXIT
( cd "$UNITY_REPO" && LC_ALL=C find "${ROOTS[@]}" -type f | LC_ALL=C sort ) > "$LIST"
COUNT="$(wc -l < "$LIST" | tr -d ' ')"
echo "── 同步原始碼 ⇒ developer@$HOST:$REMOTE（$COUNT 檔）──"

tar czf - -C "$UNITY_REPO" "${ROOTS[@]}" \
  | sshpass -e ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "developer@$HOST" \
      "mkdir -p '$REMOTE/BlueAgentUnity' && tar xzf - -C '$REMOTE'"

# 鏡像的刪除那一半：清單傳過去，讓建置機自己比對（它才知道自己手上有什麼）
sshpass -e scp -q "$LIST" "developer@$HOST:/tmp/ba-unity-files.list"
sshpass -e scp -q "$HERE/mirror_cleanup_remote.sh" "developer@$HOST:/tmp/ba-unity-mirror.sh"
sshpass -e ssh -o ConnectTimeout=30 "developer@$HOST" \
  "bash /tmp/ba-unity-mirror.sh '$REMOTE' /tmp/ba-unity-files.list ${MIRROR_DRY:+--dry-run}"

# Live2D 支援於 2026-10-30 整條從建置裡移除，但 SDK 還在倉庫裡（同步會帶過去）⇒ 每次推完照舊清掉，
# 免得殘留腳本引用 SDK 型別把舊碼編進包。倉庫那邊清乾淨後，這一行就可以一起拿掉。
sshpass -e ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "developer@$HOST" \
  "cd '$REMOTE/BlueAgentUnity/Assets' && bash -c 'shopt -s nullglob; rm -rf Live2D Live2D.meta'"
echo "── 同步完成（Mac 上那份是工作區；改東西改 NAS 這一份再重跑）──"
