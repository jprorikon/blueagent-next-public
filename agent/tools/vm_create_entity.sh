#!/bin/bash
# 建一台「她的 Ubuntu」（owner 2026-11-03：三台 VM，2 核 4 GB）。
# 用法：sudo bash agent/tools/vm_create_entity.sh mira [nori|aster]
# 重跑安全：已存在就只啟動它。
set -uo pipefail
ENTITY="${1:?entity}"
BASE=/var/lib/libvirt/images/ba-base
IMG="$BASE/noble-server-cloudimg-amd64.img"
WORK=/var/lib/libvirt/images/ba-$ENTITY
KEYS=/opt/blueagent-next/blueagent-next/data/vm/keys
LOG=/var/tmp/ba-vm
mkdir -p "$WORK" "$KEYS" "$LOG"
chmod 700 "$KEYS"

if [ ! -s "$IMG" ]; then echo "缺基礎映像 ⇒ 先跑 vm_setup_base.sh"; exit 2; fi

# ① 每台的 SSH 金鑰（引擎用它連進去；金鑰不進版控）
if [ ! -s "$KEYS/$ENTITY.key" ]; then
  ssh-keygen -t ed25519 -N '' -C "blueagent-$ENTITY" -f "$KEYS/$ENTITY.key" >/dev/null
  echo "  金鑰：$KEYS/$ENTITY.key"
fi
PUB="$(cat "$KEYS/$ENTITY.key.pub")"

# ② 磁碟（overlay，母映像保持唯讀）— 20 GB 精簡
DISK="$WORK/disk.qcow2"
if [ ! -s "$DISK" ]; then
  qemu-img create -q -f qcow2 -F qcow2 -b "$IMG" "$DISK" 20G || exit 3
  echo "  磁碟：$DISK（overlay 20G）"
fi

# ③ cloud-init seed（NoCloud）：建帳號、放公鑰、裝最小工具
SEED="$WORK/seed.iso"
cat >"$WORK/user-data" <<YAML
#cloud-config
hostname: ba-$ENTITY
users:
  - name: $ENTITY
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
      - $PUB
package_update: true
packages:
  - qemu-guest-agent
  - python3
  - python3-pip
  - git
  - curl
  - ffmpeg
# ⚠️ 不要寫 chromium-browser：Ubuntu 24.04 那是 snap 轉包，cloud 映像沒有 snapd
#    ⇒ apt 卡在 dpkg lock 十幾分鐘、cloud-init 一直 running（2026-11-03 實測）。
#    瀏覽器改用 Playwright 自帶的 chromium（tools/vm_provision.sh）。
runcmd:
  - systemctl enable --now qemu-guest-agent
  - usermod -aG sudo $ENTITY
YAML
cat >"$WORK/meta-data" <<YAML
instance-id: ba-$ENTITY-1
local-hostname: ba-$ENTITY
YAML
if [ ! -s "$SEED" ]; then
  cloud-localds "$SEED" "$WORK/user-data" "$WORK/meta-data" || exit 4
  echo "  seed：$SEED"
fi

# ④ domain（NAT、virtio、無 GUI；不設 autostart）
DOM=ba-$ENTITY
if ! virsh nwfilter-list 2>/dev/null | grep -q blueagent-vm-egress; then
  echo "缺出站隔離 filter ⇒ 先跑 tools/vm_isolate_net.sh（它會定義 blueagent-vm-egress）"; exit 6
fi
if virsh dominfo "$DOM" >/dev/null 2>&1; then
  echo "  已定義：$DOM（$(virsh domstate "$DOM")）"
else
  cat >"$WORK/domain.xml" <<XML
<domain type='kvm'>
  <name>$DOM</name>
  <memory unit='MiB'>4096</memory>
  <vcpu>2</vcpu>
  <os><type arch='x86_64'>hvm</type><boot dev='hd'/></os>
  <features><acpi/><apic/></features>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' discard='unmap'/>
      <source file='$DISK'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='$SEED'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>
    <interface type='network'>
      <source network='default'/>
      <!-- 出站隔離：能上網、碰不到內網（filter 由 tools/vm_isolate_net.sh 定義；沒定義過 define 會失敗） -->
      <filterref filter='blueagent-vm-egress'/>
      <model type='virtio'/>
    </interface>
    <serial type='file'><source path='$LOG/$ENTITY-serial.log'/><target port='0'/></serial>
    <console type='file'><source path='$LOG/$ENTITY-console.log'/><target type='serial' port='0'/></console>
    <channel type='unix'>
      <target type='virtio' name='org.qemu.guest_agent.0'/>
    </channel>
    <graphics type='vnc' port='-1' autoport='yes' listen='127.0.0.1'/>
  </devices>
</domain>
XML
  virsh define "$WORK/domain.xml" >/dev/null || exit 5
  echo "  定義：$DOM（2 vCPU／4 GB／NAT）"
fi

virsh start "$DOM" >/dev/null 2>&1
echo "  狀態：$(virsh domstate "$DOM")"
echo "  等 IP…"
for _ in $(seq 1 30); do
  IP="$(virsh domifaddr "$DOM" 2>/dev/null | awk '/ipv4/{print $4}' | cut -d/ -f1 | head -1)"
  [ -n "$IP" ] && break
  sleep 3
done
echo "  IP：${IP:-（還沒拿到，看 $LOG/$ENTITY-serial.log）}"
echo "$IP" >"$WORK/ip.txt"
