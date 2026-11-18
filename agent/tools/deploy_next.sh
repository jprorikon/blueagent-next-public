#!/usr/bin/env bash
# 建置＋上線 blueagent-next，並把當下的 git 提交戳進 image label。
#
#   bash agent/tools/deploy_next.sh
#
# 之後要確認「線上是不是庫版本」只要一行（把 <SHA> 換成部署輸出的值）：
#   sudo docker image inspect blueagent-next:<SHA> --format '{{index .Config.Labels "blueagent.git.sha"}}'
# 镜像 tag 与 label 都等于 git rev-parse HEAD（或 HEAD-dirty），避免误用旧 latest。
set -euo pipefail
cd "$(dirname "$0")/../.."   # 倉庫根（docker-compose.yml 在這）

SUDO=""
docker info >/dev/null 2>&1 || SUDO="sudo"

SHA=$(git -c safe.directory="$PWD" rev-parse HEAD)
# 只有「會被 COPY 進 image 的程式路徑」變髒才算 dirty（docs/ 不打進 image，不算）
if [ -n "$(git -c safe.directory="$PWD" status --porcelain -- agent app deploy docker-compose.yml)" ]; then
  SHA="${SHA}-dirty"
fi
export GIT_SHA="$SHA"

echo "== 建置（GIT_SHA=$GIT_SHA）"
$SUDO env GIT_SHA="$GIT_SHA" docker compose up -d --build

echo "== 上線後核對"
for container in blueagent-next blueagent-app-server blueagent-app-gateway; do
  $SUDO docker inspect "$container" --format 'container={{.Name}} status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}} started={{.State.StartedAt}}'
done
for image in "blueagent-next:$SHA" "blueagent-app-server:$SHA"; do
  $SUDO docker image inspect "$image" --format 'image {{.RepoTags}} git.sha={{index .Config.Labels "blueagent.git.sha"}}'
done
echo "期待值（本機庫）：$SHA"
# 容器起來後要暖機（載模型／連 Discord）才會 listen，這裡等最多 90 秒。
# 18790 是统一入口：/api 到 Agent，/health 到独立 App Server。
python3 - <<'PY'
import json, time, urllib.request
checks = {
    "agent": "http://127.0.0.1:18790/api/health",
    "app-server": "http://127.0.0.1:18790/health",
}
deadline = time.time() + 90
last = ""
while time.time() < deadline:
    try:
        for name, url in checks.items():
            d = json.load(urllib.request.urlopen(url, timeout=5))
            if not d.get("ok"):
                raise RuntimeError(f"{name} health not ok")
            print("%s health ok=True service=%s" % (name, d.get("engine") or d.get("service")))
        break
    except Exception as e:
        last = repr(e)
        time.sleep(3)
else:
    raise SystemExit("health 90 秒沒起來（最後錯誤：%s）——檢查三個容器日誌" % last)
PY
