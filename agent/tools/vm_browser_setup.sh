#!/bin/bash
# 把瀏覽器環境推進每台 VM（從引擎容器發起，容器裡有金鑰與 ssh）
# 用法：bash agent/tools/vm_browser_setup.sh [mira nori aster]
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ "$#" -gt 0 ]; then ents=("$@"); else ents=(mira nori aster); fi
for e in "${ents[@]}"; do
  ip="$(python3 -c "import json;print(json.load(open('/app/data/vm/$e.json'))['ip'])")"
  echo "== $e ($ip) =="
  ssh -i "/app/data/vm/keys/$e.key" -o StrictHostKeyChecking=no -o BatchMode=yes "$e@$ip" true 2>/dev/null \
    || { echo "  連不上，跳過"; continue; }
  scp -q -i "/app/data/vm/keys/$e.key" -o StrictHostKeyChecking=no "$HERE/vm_browser_install.sh" "$HERE/vm_web_cli.py" "$e@$ip:/tmp/" \
    || { echo "  上傳失敗"; continue; }
  ssh -i "/app/data/vm/keys/$e.key" -o StrictHostKeyChecking=no -o BatchMode=yes "$e@$ip" \
      'mv /tmp/vm_web_cli.py /tmp/vm_web.py && bash /tmp/vm_browser_install.sh 2>&1 | tail -12'
done
