#!/bin/bash
# 給每台 VM 固定 IP（NAT 網段），並把「角色 → IP／金鑰」寫進 data/vm/<entity>.json 給引擎用。
# 為什麼要固定：引擎靠 SSH 進她的 VM，DHCP 換 IP 就斷線；NAT 網段是我們自己建的，可以放心寫死。
set -uo pipefail
KEYS=/opt/blueagent-next/blueagent-next/data/vm/keys
OUT=/opt/blueagent-next/blueagent-next/data/vm
mkdir -p "$OUT"
declare -A FIXED=( [mira]=192.168.122.11 [nori]=192.168.122.12 [aster]=192.168.122.13 )

for e in "${!FIXED[@]}"; do
  want="${FIXED[$e]}"
  ip="$(virsh domifaddr "ba-$e" 2>/dev/null | awk '/ipv4/{print $4}' | cut -d/ -f1 | head -1)"
  echo "== $e（現在 $ip → 固定 $want）=="
  [ -z "$ip" ] && { echo "  沒有 IP，跳過"; continue; }
  iface="$(timeout 20 ssh -q -i "$KEYS/$e.key" -o StrictHostKeyChecking=no \
           -o UserKnownHostsFile=/dev/null "$e@$ip" \
           'ip -br link | awk "\$1!=\"lo\"{print \$1; exit}"' 2>/dev/null | tr -d '\r')"
  echo "  網卡：${iface:-?}"
  [ -z "$iface" ] && continue

  cat >/tmp/netplan-$e.yaml <<EOF
network:
  version: 2
  ethernets:
    $iface:
      dhcp4: false
      addresses: [$want/24]
      routes:
        - to: default
          via: 192.168.122.1
      nameservers:
        addresses: [192.168.122.1, 1.1.1.1]
EOF
  timeout 60 scp -q -i "$KEYS/$e.key" -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null /tmp/netplan-$e.yaml "$e@$ip:/tmp/99-static.yaml" || {
        echo "  scp 失敗"; continue; }
  timeout 120 ssh -q -i "$KEYS/$e.key" -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null "$e@$ip" \
      'echo "network: {config: disabled}" | sudo tee /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg >/dev/null;
       sudo cp /tmp/99-static.yaml /etc/netplan/99-static.yaml; sudo chmod 600 /etc/netplan/99-static.yaml;
       sudo netplan apply 2>&1 | tail -2; sleep 3; ip -br addr show | grep '"$want"' || true' 2>&1 | tail -4

  # 寫給引擎看的座標（金鑰路徑是容器內路徑）
  cat >"$OUT/$e.json" <<EOF
{
  "entity": "$e",
  "vm": "ba-$e",
  "ip": "$want",
  "user": "$e",
  "key": "/app/data/vm/keys/$e.key",
  "spec": {"vcpu": 2, "memory_mb": 4096, "disk_gb": 20},
  "note": "NAT 網段（libvirt default），只從宿主机／引擎容器可達；不掛任何 NAS 目錄"
}
EOF
  echo "  座標已寫：$OUT/$e.json"
  # 引擎的 `terminal` 用 accept-new 記住主機金鑰；重建過的 VM 金鑰會變 ⇒ 清掉舊的那行，下次連線重新記
  ssh-keygen -q -f "$OUT/known_hosts" -R "$want" >/dev/null 2>&1 || true
done

echo "== 連線複驗 =="
for e in "${!FIXED[@]}"; do
  printf "%-8s " "$e"
  timeout 20 ssh -q -i "$KEYS/$e.key" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
     -o ConnectTimeout=8 "$e@${FIXED[$e]}" 'echo "$(whoami)@$(hostname) $(lsb_release -ds 2>/dev/null)"' 2>&1 | tail -1
done
