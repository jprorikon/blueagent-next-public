#!/bin/bash
# 一台 VM 的前置：NAT 網路 ＋ 基礎映像 ＋ 工具（owner 2026-11-03：三台 Ubuntu 給她們用）
# 只做一次；重跑是安全的（已存在就跳過）。
set -uo pipefail
BASE=/var/lib/libvirt/images/ba-base
LOG=/var/tmp/ba-vm
mkdir -p "$BASE" "$LOG"

echo "== 1. 工具 =="
if ! command -v cloud-localds >/dev/null || ! command -v genisoimage >/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y cloud-image-utils genisoimage >$LOG/apt.log 2>&1
  echo "  apt: exit=$?（log $LOG/apt.log）"
else
  echo "  已有"
fi

echo "== 2. NAT 網路（default，192.168.122.0/24）=="
if virsh net-info default >/dev/null 2>&1; then
  active="$(virsh net-info default | awk '/^Active/{print $2}')"
  echo "  已定義（Active=$active）"
  if [ "$active" != "yes" ]; then virsh net-start default >/dev/null 2>&1 && echo "  start ok"; fi
else
  cat >/tmp/ba-net.xml <<'XML'
<network>
  <name>default</name>
  <forward mode='nat'/>
  <bridge name='virbr0' stp='on' delay='0'/>
  <ip address='192.168.122.1' netmask='255.255.255.0'>
    <dhcp>
      <range start='192.168.122.100' end='192.168.122.200'/>
    </dhcp>
  </ip>
</network>
XML
  virsh net-define /tmp/ba-net.xml >/dev/null && echo "  define ok"
  virsh net-start default >/dev/null && echo "  start ok"
  # 刻意**不**設 autostart：owner 的規矩是這台機器別多常駐（記憶體吃緊）
fi

echo "== 3. 基礎映像（Ubuntu 24.04 cloud image）=="
IMG="$BASE/noble-server-cloudimg-amd64.img"
if [ -s "$IMG" ]; then
  echo "  已有 $(du -h "$IMG" | cut -f1)"
else
  # 官方 currently/ 指向最新 24.04；約 600 MB
  curl -fSL --retry 3 -o "$IMG.part" \
    https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img \
    >$LOG/img.log 2>&1
  code=$?
  if [ $code -ne 0 ] || [ ! -s "$IMG.part" ]; then
    echo "  下載失敗 exit=$code（log $LOG/img.log）"
    rm -f "$IMG.part"
    exit 3
  fi
  mv "$IMG.part" "$IMG"
  echo "  下載完成 $(du -h "$IMG" | cut -f1)"
fi
qemu-img info "$IMG" | head -4
echo "== 完成 =="
