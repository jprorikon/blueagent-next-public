#!/bin/bash
# 收尾三台 VM：停掉卡住的 cloud-init apt、裝最小工具、裝 Playwright chromium
# 坑：cloud-init 的 packages 裡有 chromium-browser —— Ubuntu 24.04 那是 snap 轉包，
#     cloud 映像沒有 snapd ⇒ apt 卡在 dpkg lock 十分鐘以上（實測）。
#     正解：不要 snap，用 Playwright 自帶的 chromium。
set -uo pipefail
KEYS=/opt/blueagent-next/blueagent-next/data/vm/keys
for e in "$@"; do
  ip="$(virsh domifaddr "ba-$e" 2>/dev/null | awk '/ipv4/{print $4}' | cut -d/ -f1 | head -1)"
  echo "== $e ($ip) =="
  [ -z "$ip" ] && continue
  timeout 900 ssh -q -i "$KEYS/$e.key" -o StrictHostKeyChecking=no \
     -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8 "$e@$ip" \
     'set -u
      # ① 放掉卡住的 apt（cloud-init 裝 snap 轉包）
      sudo pkill -f "apt-get" 2>/dev/null; sleep 2
      sudo dpkg --configure -a >/dev/null 2>&1 || true
      # ② 最小工具（不含 chromium-browser）
      sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        python3-venv python3-pip git curl ffmpeg qemu-guest-agent >/dev/null 2>&1
      echo "  套件：$(python3 -V 2>&1) / venv：$(python3 -c "import venv;print(\"ok\")" 2>&1)"
      # ③ Playwright chromium（headless 可用，自带 ~150 个依赖）
      rm -rf "$HOME/.browser"
      python3 -m venv "$HOME/.browser" >/dev/null 2>&1
      "$HOME/.browser/bin/pip" -q install --upgrade pip >/dev/null 2>&1
      "$HOME/.browser/bin/pip" -q install playwright >/dev/null 2>&1
      "$HOME/.browser/bin/playwright" install --with-deps chromium >/tmp/pw.log 2>&1
      echo "  playwright：exit=$? ($(tail -1 /tmp/pw.log | cut -c1-80))"
      "$HOME/.browser/bin/python" - <<'"'"'PY'"'"'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page()
    page.goto("https://example.com", timeout=20000)
    print("  browser ok:", b.version, "| title:", page.title())
    b.close()
PY' 2>&1 | tail -8
done
