#!/usr/bin/env python3
"""把真相的 SVG 圖示（`index.html` 的 `<symbol id="i-*">`）烤成 PNG ⇒ `Assets/Resources/icons/`。

為什麼要烤：真相那一側的圖示是 SVG（`.ico { fill:none; stroke:currentColor; stroke-width:2;
stroke-linecap:round; stroke-linejoin:round }`）。Unity 沒有 SVG 管線 ⇒ 照**貼圖／頭像同一條路**
打包成 PNG，Unity 那一側用 `RawImage` 貼上去、顏色自己 tint（PNG 烤成白色）。
**不要自己重打 path**：用真相同一套 SVG／CSS 烤出來，線寬／圓角／端點才會逐像素同源。

跑法（要 `.venv-cdp`，`Cdp` 直接吃 `tools/webui_dump.py` 那一支）：
    /vol1/.../.venv-cdp/bin/python tools/bake_icons.py [--size 48] [--scale 2]

⚠️ 進版控的是**烤好的 PNG**（跟貼圖 PNG／字型同一類：建置的輸入）；這支腳本是重烤用的。
   圖示改了（index.html 的 symbol）⇒ 重跑本支 + 一起入庫。
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WEBUI = os.path.join(os.path.dirname(ROOT), "live2d", "assets", "webui")
OUT = os.path.join(ROOT, "BlueAgentUnity", "Assets", "Resources", "icons")

# 真相那一側用 `.ico-fill`（`fill:currentColor; stroke:none`＝**實心**）的圖示 ⇒ 另外烤一份
# `i-<name>-fill.png`（`index.html` 的 `.status-prefix` #i-dot／`.momotalk-mark` #i-pawn，
# `app.js` 的 `.media-badge` #i-play）。
FILLED = ["dot", "pawn", "play"]

sys.path.insert(0, HERE)
from webui_dump import Cdp, CHROME          # noqa: E402  （同一支 CDP 客戶端／同一個 Chrome 路徑）


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def symbols():
    html = open(os.path.join(WEBUI, "index.html"), encoding="utf-8").read()
    return sorted(set(re.findall(r'<symbol id="(i-[a-z0-9-]+)"', html)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=float, default=48.0, help="烤的邊長（CSS px；預設 48）")
    ap.add_argument("--scale", type=float, default=2.0, help="CDP 截圖倍率（預設 2 ⇒ 96px PNG）")
    args = ap.parse_args()

    names = symbols()
    if not names:
        raise SystemExit(f"index.html 裡找不到 <symbol id=\"i-*\">：{WEBUI}")
    os.makedirs(OUT, exist_ok=True)

    sport, cport = free_port(), free_port()
    udir = tempfile.mkdtemp(prefix="ba-bake-icons-")
    srv = subprocess.Popen([sys.executable, "-m", "http.server", str(sport),
                            "--bind", "127.0.0.1", "--directory", WEBUI],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
         "--remote-allow-origins=*",
         f"--remote-debugging-port={cport}", f"--user-data-dir={udir}",
         "--window-size=460,818", "--force-device-scale-factor=1", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        cdp = Cdp(cport)
        if not cdp.connect("about:blank"):
            raise SystemExit("連不上無頭瀏覽器（CDP 沒起來）")
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        # 透明底：不然截圖會把外殼漸層一起烤進去
        cdp.send("Emulation.setDefaultBackgroundColorOverride",
                 color={"r": 0, "g": 0, "b": 0, "a": 0})
        cdp.send("Emulation.setDeviceMetricsOverride", width=520, height=520, deviceScaleFactor=1,
                 mobile=False)
        cdp.send("Page.navigate", url=f"http://127.0.0.1:{sport}/index.html")
        for _ in range(60):
            if cdp.eval("document.readyState") == "complete":
                break
            time.sleep(0.1)
        # 只留 <symbol> 那一支 sprite ＋ 我們自己排的烤盤，其餘（外殼、對話…）全部收起來
        js = """
        (function(){
          var sprite = null;
          document.querySelectorAll('body > *').forEach(function (el) {
            if (el.tagName.toLowerCase() === 'svg' && el.querySelector('symbol')) sprite = el;
          });
          document.querySelectorAll('body > *').forEach(function (el) {
            if (el !== sprite) el.style.display = 'none';
          });
          var names = %s, filled = %s, sz = %s, html = '';
          function one(id, n, cls) {
            return '<svg class="ico ' + cls + '" id="bk-' + id + '" style="display:block;width:' +
                   sz + 'px;height:' + sz + 'px;color:#fff;margin:0"><use href="#' + n +
                   '"></use></svg>';
          }
          names.forEach(function (n) {
            html += one(n, n, '');
            if (filled.indexOf(n.replace('i-', '')) >= 0) html += one(n + '-fill', n, 'ico-fill');
          });
          var b = document.createElement('div');
          b.id = 'bake';
          b.style.cssText = 'position:fixed;left:0;top:0;z-index:99999;background:transparent';
          b.innerHTML = html;
          document.body.appendChild(b);
          if (sprite) { sprite.style.display = 'block';
                        sprite.style.position = 'absolute'; sprite.style.left = '-9999px'; }
          return 'ok';
        })()
        """ % (json.dumps(names), json.dumps(FILLED), json.dumps(args.size))
        cdp.eval(js)
        cdp.eval("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")

        targets = list(names) + [f"i-{f}-fill" for f in FILLED if f"i-{f}" in names]
        made = []
        for name in targets:
            r = json.loads(cdp.eval(
                f"(function(){{var e=document.getElementById('bk-{name}');"
                f"var b=e.getBoundingClientRect();"
                f"return JSON.stringify({{x:b.x,y:b.y,w:b.width,h:b.height}});}})()"))
            rid = cdp.send("Page.captureScreenshot", format="png", captureBeyondViewport=True,
                           clip={"x": r["x"], "y": r["y"], "width": r["w"], "height": r["h"],
                                 "scale": args.scale})
            shot = cdp.wait(rid) or {}
            data = ((shot.get("result") or {}).get("data"))
            if not data:
                raise SystemExit(f"截圖失敗：{name}（{shot}）")
            path = os.path.join(OUT, f"{name}.png")
            with open(path, "wb") as f:
                f.write(base64.b64decode(data))
            made.append((name, path, os.path.getsize(path)))
    finally:
        for p in (chrome, srv):
            try:
                p.terminate()
            except Exception:
                pass

    for name, path, size in made:
        print(f"  {name:22s} {size:7d} bytes  {path}")
    print(f"共 {len(made)} 張 ⇒ {OUT}")


if __name__ == "__main__":
    main()
