#!/bin/bash
# 給每台 VM 加 2 GB swap（實測：她們自己看機器時都注意到「swap が無い」）
set -uo pipefail
KEYS=/opt/blueagent-next/blueagent-next/data/vm/keys
for e in "$@"; do
  ip="$(python3 -c "import json;print(json.load(open('/opt/blueagent-next/blueagent-next/data/vm/'+__import__('sys').argv[1]+'.json'))['ip'])" "$e" 2>/dev/null)"
  [ -z "$ip" ] && { echo "$e：沒 IP"; continue; }
  printf "%-8s " "$e"
  timeout 300 ssh -q -i "$KEYS/$e.key" -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null "$e@$ip" 'set -e
      if ! sudo swapon --show | grep -q /swapfile; then
        sudo fallocate -l 2G /swapfile 2>/dev/null || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
        sudo chmod 600 /swapfile; sudo mkswap /swapfile >/dev/null; sudo swapon /swapfile
        grep -q "^/swapfile" /etc/fstab || echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab >/dev/null
      fi
      free -m | awk "/Swap/{printf \"swap=%sMB used=%sMB\\n\", \$2, \$3}"' 2>&1 | tail -1
done
