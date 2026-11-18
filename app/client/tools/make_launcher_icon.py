#!/usr/bin/env python3
"""把 `tools/icon_src/blueagent_logo.webp`（owner 2026-10-20 给的标志）烤成 Android 桌面图标资源。

产出（全部进版控，是**建置的输入**）：
  res/mipmap-<dpi>/ic_launcher.png             legacy 方形：白底 + 标志占宽 82%
  res/mipmap-<dpi>/ic_launcher_round.png       同上（圆形启动器会自己裁）
  res/mipmap-<dpi>/ic_launcher_foreground.png  adaptive 前景：透明底，标志占宽 58%（落在 66dp 安全区）
另外产出预览（不进版控，只给肉眼/VL 复核）：
  /tmp/ba_icon_legacy_1024.png、/tmp/ba_icon_adaptive_preview.png

跑法：`.venv-yolo/bin/python tools/make_launcher_icon.py`（需要 Pillow）
为什么留着这支：图标是**烤出来的资产**（不是手绘 XML）——换标志就换 icon_src 那张图重跑，
不要在 res/ 里手动改 PNG。

⚠️ 源图是「白底 + 边缘灰晕」、没有阴影 ⇒ 背景判定＝「低饱和（max-min<26）且亮（lum>=165）」，
再按亮度做 45 阶 alpha 过渡保留抗锯齿边。若换了别的底（透明/彩色），这段判定要跟着改。
"""
import os
import sys

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SRC = os.path.join(HERE, 'icon_src', 'blueagent_logo.webp')
RES = os.path.join(REPO, 'BlueAgentUnity', 'Assets', 'Plugins', 'Android',
                   'blueagent-permissions.androidlib', 'res')
DENS = {'mdpi': 48, 'hdpi': 72, 'xhdpi': 96, 'xxhdpi': 144, 'xxxhdpi': 192}


def extract_logo(path):
    """裁到墨迹包围盒，并把背景（白 + 灰晕）压成透明。"""
    im = Image.open(path).convert('RGB')
    w, h = im.size
    px = im.load()

    def is_ink(r, g, b):
        mx, mn = max(r, g, b), min(r, g, b)
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        return (mx - mn) >= 40 or lum <= 150      # 彩度高（蓝） 或 很暗（黑字/环）

    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        for x in range(w):
            if is_ink(*px[x, y]):
                minx, miny = min(minx, x), min(miny, y)
                maxx, maxy = max(maxx, x), max(maxy, y)
    if maxx < 0:
        sys.exit('找不到墨迹：源图是不是全白？')
    m = max(6, int((maxx - minx) * 0.03))
    box = (max(0, minx - m), max(0, miny - m), min(w, maxx + m + 1), min(h, maxy + m + 1))

    logo = im.crop(box).convert('RGBA')
    lw, lh = logo.size
    lp = logo.load()
    for y in range(lh):
        for x in range(lw):
            r, g, b, _ = lp[x, y]
            mx, mn = max(r, g, b), min(r, g, b)
            lum = 0.299 * r + 0.587 * g + 0.114 * b
            if (mx - mn) < 26 and lum >= 165:         # 背景（含灰晕）
                lp[x, y] = (255, 255, 255, 0)
            elif (mx - mn) < 26 and lum >= 120:       # 抗锯齿过渡
                lp[x, y] = (r, g, b, max(0, min(255, int((165 - lum) / 45 * 255))))
            else:
                lp[x, y] = (r, g, b, 255)
    return logo


def scale_to_width(img, target_w):
    w, h = img.size
    return img.resize((int(target_w), max(1, int(h * target_w / w))), Image.LANCZOS)


def legacy(logo, canvas, fill=0.82):
    out = Image.new('RGB', (canvas, canvas), (255, 255, 255))
    lg = scale_to_width(logo, int(canvas * fill))
    out.paste(lg, ((canvas - lg.size[0]) // 2, (canvas - lg.size[1]) // 2), lg)
    return out


def foreground(logo, canvas, fill=0.58):
    out = Image.new('RGBA', (canvas, canvas), (0, 0, 0, 0))
    lg = scale_to_width(logo, int(canvas * fill))
    out.paste(lg, ((canvas - lg.size[0]) // 2, (canvas - lg.size[1]) // 2), lg)
    return out


def ios_icon(logo, canvas=1024, fill=0.72):
    """iOS 的 App Store 图标：**必须不透明**（Apple 不收带 alpha 的），iOS 自己会切圆角。
    所以这里画在白底、存 RGB（不是 RGBA）。"""
    out = Image.new('RGB', (canvas, canvas), (255, 255, 255))
    lg = scale_to_width(logo, int(canvas * fill))
    out.paste(lg, ((canvas - lg.size[0]) // 2, (canvas - lg.size[1]) // 2), lg)
    return out


def main():
    if not os.path.exists(SRC):
        sys.exit(f'找不到源图：{SRC}')
    logo = extract_logo(SRC)
    print('logo', logo.size, 'from', os.path.basename(SRC))
    for name, size in DENS.items():
        d = os.path.join(RES, f'mipmap-{name}')
        os.makedirs(d, exist_ok=True)
        legacy(logo, size).save(os.path.join(d, 'ic_launcher.png'))
        legacy(logo, size).save(os.path.join(d, 'ic_launcher_round.png'))
        fg_size = int(round(size * 108 / 48))          # adaptive 前景＝108dp
        foreground(logo, fg_size).save(os.path.join(d, 'ic_launcher_foreground.png'))
        print('wrote', name, size, 'fg', fg_size)

    # iOS：1024×1024 不透明 PNG，Unity 建置时由 `HeadlessBuild.ConfigureIOS()` 设进 PlayerSettings
    ios_dir = os.path.join(REPO, 'BlueAgentUnity', 'Assets', 'Icons')
    os.makedirs(ios_dir, exist_ok=True)
    ios_path = os.path.join(ios_dir, 'appicon_ios_1024.png')
    ios_icon(logo, 1024).save(ios_path)
    print('wrote ios', ios_path, Image.open(ios_path).mode)


    # 预览：方形；圆形（adaptive 在 72dp 可视区里被裁的样子）
    legacy(logo, 1024).save('/tmp/ba_icon_legacy_1024.png')
    prev = Image.new('RGB', (1024, 1024), (255, 255, 255))
    f = foreground(logo, 1024)
    f2 = f.resize((1536, 1536), Image.LANCZOS)          # 108dp → 可视 72dp ⇒ 放大 1.5
    prev.paste(f2, (-256, -256), f2)
    mask = Image.new('L', (1024, 1024), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 1023, 1023), fill=255)
    circ = Image.new('RGB', (1024, 1024), (255, 255, 255))
    circ.paste(prev, (0, 0), mask)
    circ.save('/tmp/ba_icon_adaptive_preview.png')
    print('previews -> /tmp/ba_icon_legacy_1024.png, /tmp/ba_icon_adaptive_preview.png')


if __name__ == '__main__':
    main()
