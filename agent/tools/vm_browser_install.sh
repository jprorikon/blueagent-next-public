#!/bin/bash
# 在 VM 內跑：把「看網頁」這件事準備好（venv＋playwright＋`web` 指令＋PATH）
# 由 tools/vm_browser_setup.sh 推進每台 VM 執行。
set -u
echo "  [1/5] 依賴"
sudo apt-get update -qq >/dev/null 2>&1 || true
sudo apt-get install -y -qq python3-venv python3-pip >/dev/null 2>&1 || true

echo "  [2/5] venv（~/.browser-venv）"
[ -x "$HOME/.browser-venv/bin/python" ] || python3 -m venv "$HOME/.browser-venv"
"$HOME/.browser-venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true

echo "  [3/5] playwright（python 包）"
"$HOME/.browser-venv/bin/pip" install -q playwright 2>&1 | tail -2
"$HOME/.browser-venv/bin/python" -c "import playwright; print('         playwright ok')" || exit 3

echo "  [4/5] 瀏覽器（已快取就不重下）"
"$HOME/.browser-venv/bin/python" -m playwright install chromium 2>&1 | tail -2

echo "  [5/5] 安裝 web 指令"
mkdir -p "$HOME/bin"
install -m 0755 /tmp/vm_web.py "$HOME/bin/web.py"
cat > "$HOME/bin/web" <<'SH'
#!/bin/bash
exec "$HOME/.browser-venv/bin/python" "$HOME/bin/web.py" "$@"
SH
chmod +x "$HOME/bin/web"
grep -q 'HOME/bin' "$HOME/.profile" 2>/dev/null || echo 'export PATH="$HOME/bin:$PATH"' >> "$HOME/.profile"

echo "  自檢："
"$HOME/bin/web" text https://example.com --chars 200 2>&1 | head -4
