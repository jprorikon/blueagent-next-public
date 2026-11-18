#!/bin/bash
# 三台 VM 的出站隔離（2026-11-03 review）：**能上網，碰不到內網**。
#
# 為什麼要有：VM 走 libvirt NAT（192.168.122.0/24），NAT 只管「位址轉換」不管「能去哪」——
# 沒有這層的話，VM 可以直接連 192.168.0.x 整個 LAN（NAS 管理頁、Strata、TTS、
# BlueAgent API）、宿主 192.168.122.1、Tailscale（100.64/10），三台之間也互通。
# 而 VM 裡跑的是**模型生成的命令**（`terminal` 工具）＋ 會開網頁的瀏覽器 ⇒ 網頁裡的注入
# 就能變成在內網發請求。這裡用 libvirt nwfilter 掛在每台的網卡上（跟著 domain XML 走，
# 不受 iptables/nftables 後端切換影響）：
#   · 引擎 ssh 進來的回包（ESTABLISHED/RELATED）放行
#   · DNS 只准到閘道 192.168.122.1:53
#   · 新連線到 RFC1918／CGNAT／link-local 一律丟掉（含宿主、含另外兩台）
#   · 其餘（公網）放行：apt／pip／git／瀏覽器照常
#
# 用法：sudo bash agent/tools/vm_isolate_net.sh            # 定義 filter、掛到三台（下次開機生效）
#       sudo bash agent/tools/vm_isolate_net.sh --restart  # 同上，並重開三台立即生效（會中斷她們手上的事）
#       sudo bash agent/tools/vm_isolate_net.sh --check    # 只驗證：內網應該連不到、公網應該連得到
# 重跑安全：filter 會被覆寫成同一份、已掛過的網卡不會重複掛。
set -uo pipefail
FILTER=blueagent-vm-egress
KEYS=/opt/blueagent-next/blueagent-next/data/vm/keys
KNOWN=/opt/blueagent-next/blueagent-next/data/vm/known_hosts
declare -A FIXED=( [mira]=192.168.122.11 [nori]=192.168.122.12 [aster]=192.168.122.13 )
MODE="${1:-}"

check() {
  echo "== 驗證（每台：內網要 BLOCKED、公網要 OK、ssh 本身要通）=="
  for e in "${!FIXED[@]}"; do
    printf "%-8s " "$e"
    timeout 40 ssh -q -i "$KEYS/$e.key" -o StrictHostKeyChecking=accept-new \
        -o UserKnownHostsFile="$KNOWN" -o ConnectTimeout=8 "$e@${FIXED[$e]}" '
      probe() { if timeout 4 bash -c "</dev/tcp/$1/$2" 2>/dev/null; then echo -n "$3=OPEN "; else echo -n "$3=BLOCKED "; fi; }
      probe 192.168.122.1 22 host
      probe 192.168.50.36 8080 strata
      probe 192.168.50.32 9881 tts
      for peer in 192.168.122.11 192.168.122.12 192.168.122.13; do
        [ "$(hostname -I | tr -d " ")" = "$peer" ] || probe $peer 22 peer-${peer##*.}
      done
      if timeout 8 curl -fsS -o /dev/null https://example.com; then echo "internet=OK"; else echo "internet=FAIL"; fi
    ' 2>&1 | tail -1 || echo "（ssh 不通——先查 filter 是不是把回包也擋了）"
  done
}

if [ "$MODE" = "--check" ]; then check; exit 0; fi

echo "== 1. 定義 nwfilter：$FILTER =="
TMP="$(mktemp)"
cat >"$TMP" <<XML
<filter name='$FILTER' chain='root' priority='-650'>
  <!-- 引擎 ssh 進來那條連線的回包 -->
  <rule action='accept' direction='out' priority='100'>
    <all state='ESTABLISHED,RELATED'/>
  </rule>
  <!-- DNS 只到閘道（dnsmasq） -->
  <rule action='accept' direction='out' priority='200'>
    <udp dstipaddr='192.168.122.1' dstportstart='53'/>
  </rule>
  <rule action='accept' direction='out' priority='200'>
    <tcp dstipaddr='192.168.122.1' dstportstart='53'/>
  </rule>
  <!-- 私網／CGNAT（Tailscale）／link-local：新連線一律丟 -->
  <rule action='drop' direction='out' priority='300'><all dstipaddr='10.0.0.0' dstipmask='8'/></rule>
  <rule action='drop' direction='out' priority='300'><all dstipaddr='172.16.0.0' dstipmask='12'/></rule>
  <rule action='drop' direction='out' priority='300'><all dstipaddr='192.168.50.0' dstipmask='16'/></rule>
  <rule action='drop' direction='out' priority='300'><all dstipaddr='100.64.0.0' dstipmask='10'/></rule>
  <rule action='drop' direction='out' priority='300'><all dstipaddr='169.254.0.0' dstipmask='16'/></rule>
</filter>
XML
virsh nwfilter-define "$TMP" >/dev/null || { echo "  nwfilter-define 失敗"; rm -f "$TMP"; exit 2; }
rm -f "$TMP"
echo "  已定義"

echo "== 2. 掛到每台的網卡（持久設定）=="
for e in "${!FIXED[@]}"; do
  DOM=ba-$e
  if ! virsh dominfo "$DOM" >/dev/null 2>&1; then echo "  $DOM：不存在，跳過"; continue; fi
  XMLF="$(mktemp)"
  virsh dumpxml --inactive "$DOM" >"$XMLF"
  if grep -q "filterref filter='$FILTER'" "$XMLF"; then
    echo "  $DOM：已掛過"
  else
    # 在 default 網路那張網卡的 <source network='default'/> 後面插一行 filterref
    sed -i "s|<source network='default'/>|&\n      <filterref filter='$FILTER'/>|" "$XMLF"
    if grep -q "filterref filter='$FILTER'" "$XMLF" && virsh define "$XMLF" >/dev/null; then
      echo "  $DOM：已掛上（下次開機生效）"
    else
      echo "  $DOM：掛不上（網卡不是 default 網路？手動 virsh edit $DOM）"
    fi
  fi
  rm -f "$XMLF"
  if [ "$MODE" = "--restart" ] && [ "$(virsh domstate "$DOM")" = "running" ]; then
    virsh shutdown "$DOM" >/dev/null
    for _ in $(seq 1 40); do [ "$(virsh domstate "$DOM")" = "shut off" ] && break; sleep 2; done
    [ "$(virsh domstate "$DOM")" = "shut off" ] || virsh destroy "$DOM" >/dev/null
    virsh start "$DOM" >/dev/null && echo "  $DOM：已重開"
  fi
done

if [ "$MODE" = "--restart" ]; then
  echo "  等開機…"; sleep 30
  check
else
  echo "要立即生效：sudo bash agent/tools/vm_isolate_net.sh --restart（或各台自己重開後跑 --check）"
fi
