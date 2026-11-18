#!/bin/bash
# 在 VM 內跑（由 vm_push_provision.sh 推進來）：最小工具 ＋ Playwright chromium
set -u
echo "  [1/4] 放掉卡住的 apt"
sudo pkill -f "apt-get" 2>/dev/null || true
sleep 2
sudo dpkg --configure -a >/dev/null 2>&1 || true

echo "  [2/4] 最小工具"
sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || true
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3-venv python3-pip git curl ffmpeg qemu-guest-agent >/dev/null 2>&1
printf "         python=%s venv=%s\n" "$(python3 -V 2>&1 | cut -d' ' -f2)" \
  "$(python3 -c 'import venv; print("ok")' 2>&1)"

echo "  [3/4] Playwright chromium"
rm -rf "$HOME/.browser"
python3 -m venv "$HOME/.browser" >/dev/null 2>&1
if [ ! -x "$HOME/.browser/bin/pip" ]; then
  echo "         venv 建不起來（python3-venv 沒裝好）"; exit 3
fi
"$HOME/.browser/bin/pip" -q install --upgrade pip >/dev/null 2>&1
"$HOME/.browser/bin/pip" -q install playwright >/dev/null 2>&1
"$HOME/.browser/bin/playwright" install --with-deps chromium >/tmp/pw.log 2>&1
printf "         playwright exit=%s %s\n" "$?" "$(tail -1 /tmp/pw.log | cut -c1-70)"

echo "  [4/4] 真的開一次網頁"
"$HOME/.browser/bin/python" - <<'PY'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page()
    page.goto("https://example.com", timeout=25000)
    print("         browser ok:", b.version, "| title:", page.title())
    b.close()
PY
