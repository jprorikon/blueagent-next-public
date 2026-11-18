#!/bin/bash
# 把 tools/vm_provision.sh 推進去每台 VM 執行（避開 ssh 內嵌 heredoc 的引號地獄）
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
KEYS=/opt/blueagent-next/blueagent-next/data/vm/keys
for e in "$@"; do
  ip="$(python3 -c "import json;print(json.load(open('/opt/blueagent-next/blueagent-next/data/vm/'+__import__('sys').argv[1]+'.json'))['ip'])" "$e" 2>/dev/null)"
  echo "== $e ($ip) =="
  [ -z "$ip" ] && continue
  if timeout 60 scp -q -i "$KEYS/$e.key" -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null "$HERE/vm_provision.sh" "$e@$ip:/tmp/vm_provision.sh"; then
    echo "  推進去 ok"
  else
    echo "  scp 失敗"; continue
  fi
  timeout 1200 ssh -q -i "$KEYS/$e.key" -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null "$e@$ip" 'bash /tmp/vm_provision.sh' 2>&1 | tail -10
done
