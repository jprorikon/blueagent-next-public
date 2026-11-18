#!/bin/bash
# 三台 VM 的健檢：cloud-init 狀態、chromium、記憶體實際用量
set -uo pipefail
KEYS=/opt/blueagent-next/blueagent-next/data/vm/keys
for e in mira nori aster; do
  ip="$(virsh domifaddr ba-$e 2>/dev/null | awk '/ipv4/{print $4}' | cut -d/ -f1 | head -1)"
  printf "%-8s ip=%-16s " "$e" "${ip:-?}"
  if [ -z "$ip" ]; then echo "（沒拿到 IP）"; continue; fi
  out=$(timeout 25 ssh -q -i "$KEYS/$e.key" -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8 "$e@$ip" \
        'echo -n "cloud-init:$(cloud-init status 2>/dev/null | sed "s/status: //"); "; 
         printf "chromium:%s " "$(command -v chromium || command -v chromium-browser || echo none)";
         printf "py:%s " "$(python3 -V 2>&1 | cut -d" " -f2)";
         echo' 2>&1 | tr -d "\r")
  echo "$out"
  printf "         rss="
  virsh dommemstat "ba-$e" 2>/dev/null | awk '/^rss/{printf "%.0f MiB", $2/1024}'
  echo
done
echo "--- 主機記憶體 ---"
free -h | head -2
