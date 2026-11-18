#!/bin/bash
# 修 VM 內時鐘（1970）＋補裝 cloud-init 沒裝成的套件
# 為什麼會有 1970：domain XML 沒帶 <clock>，guest 沒有可靠 RTC ⇒ apt 走 https 時憑證檢查失敗
# ⇒ cloud-init 的 package_update/install 全部失敗、venv 也沒有 ensurepip。
set -uo pipefail
KEYS=/opt/blueagent-next/blueagent-next/data/vm/keys
NOW="$(date -u '+%Y-%m-%d %H:%M:%S')"
for e in "$@"; do
  ip="$(virsh domifaddr "ba-$e" 2>/dev/null | awk '/ipv4/{print $4}' | cut -d/ -f1 | head -1)"
  echo "== $e ($ip) =="
  [ -z "$ip" ] && continue
  timeout 600 ssh -q -i "$KEYS/$e.key" -o StrictHostKeyChecking=no \
     -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8 "$e@$ip" \
     "set -e
      echo '現在 guest 時間：'\$(date -u)
      echo '$NOW' | sudo tee /dev/null >/dev/null
      sudo date -u -s '$NOW' >/dev/null
      echo '校時後：'\$(date -u)
      sudo timedatectl set-ntp true 2>/dev/null || true
      sudo apt-get update -qq 2>&1 | tail -2
      sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv git curl ffmpeg qemu-guest-agent 2>&1 | tail -3
      echo '套件完成'
      cloud-init status --long 2>&1 | head -3" 2>&1 | tail -10
done
