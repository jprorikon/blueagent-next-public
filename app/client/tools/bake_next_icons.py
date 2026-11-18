#!/usr/bin/env python3
"""烤 Next 端新增的圖示與裝飾貼圖（2026-10-29 原創幾何風格改版）⇒ `Assets/Resources/icons/i-*.png`。

為什麼自己畫：舊的那批 `i-*.png` 是從舊 webui 的 SVG 烤的（`bake_icons.py`，要 Chrome＋CDP），
這次要補的麥克風、送出、重新整理…那邊沒有。這支用**純 Python**（不用 PIL／numpy）把同一種線描
風格畫出來：24 格座標、線寬 2、圓頭——跟舊圖示放在一起看不出兩批。
裝飾貼圖（光環、三角、閃光）是原創的幾何形狀，只取 an original geometric UI「光環＋三角碎片」的設計語言，
**不是**遊戲裡的素材。

產物：白色＋alpha 的 96×96 PNG（Unity 那側用 `UiBuilder.Icon` 貼、自己 tint）。
跑法：`python3 app/client/tools/bake_next_icons.py`（產物進版控；改了形狀就重跑）。
"""

from __future__ import annotations

import math
import pathlib
import struct
import sys
import zlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "BlueAgentUnity" / "Assets" / "Resources" / "icons"
SIZE = 96
GRID = 24.0
STROKE = 2.0
SUPERSAMPLE = 4


# ── 形狀（24 格座標；y 往下）───────────────────────────────────────────────

def line(x0, y0, x1, y1):
    return [((x0, y0), (x1, y1))]


def poly(*points, closed=False):
    pts = list(points)
    if closed:
        pts.append(pts[0])
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]


def arc(cx, cy, r, start, end, steps=48, ry=None):
    """角度用度，0°＝右、90°＝下（螢幕座標）；從 start 掃到 end。"""
    ry = r if ry is None else ry
    pts = []
    for i in range(steps + 1):
        a = math.radians(start + (end - start) * i / steps)
        pts.append((cx + r * math.cos(a), cy + ry * math.sin(a)))
    return poly(*pts)


def circle(cx, cy, r):
    return arc(cx, cy, r, 0, 360, steps=72)


def rounded_rect(x0, y0, x1, y1, r):
    return (line(x0 + r, y0, x1 - r, y0) + arc(x1 - r, y0 + r, r, -90, 0, 12)
            + line(x1, y0 + r, x1, y1 - r) + arc(x1 - r, y1 - r, r, 0, 90, 12)
            + line(x1 - r, y1, x0 + r, y1) + arc(x0 + r, y1 - r, r, 90, 180, 12)
            + line(x0, y1 - r, x0, y0 + r) + arc(x0 + r, y0 + r, r, 180, 270, 12))


ICONS = {
    # 麥克風：膠囊＋底座弧＋腳
    "mic": rounded_rect(9, 2, 15, 15, 3) + line(19, 10, 19, 11) + arc(12, 11, 7, 0, 180)
           + line(5, 11, 5, 10) + line(12, 18, 12, 22) + line(8, 22, 16, 22),
    # 送出：紙飛機
    "send": poly((22, 2), (11, 13)) + poly((22, 2), (15, 22), (11, 13), (2, 9), (22, 2)),
    "plus": line(12, 5, 12, 19) + line(5, 12, 19, 12),
    "minus": line(5, 12, 19, 12),
    "check": poly((20, 6), (9, 17), (4, 12)),
    # 重新整理：開口圓＋箭頭
    "refresh": arc(12, 12, 9, 0, 312) + poly((21, 3), (21, 8), (16, 8)),
    # 登出：門框＋箭頭
    "logout": (line(9, 21, 5, 21) + arc(5, 19, 2, 90, 180, 8) + line(3, 19, 3, 5)
               + arc(5, 5, 2, 180, 270, 8) + line(5, 3, 9, 3)
               + poly((16, 17), (21, 12), (16, 7)) + line(21, 12, 9, 12)),
    # 音量：喇叭＋兩道聲波
    "volume": (poly((11, 5), (6, 9), (2, 9), (2, 15), (6, 15), (11, 19), closed=True)
               + arc(11.93, 12, 5, -44.4, 44.4, 16) + arc(11.86, 12, 10, -44.4, 44.4, 24)),
    # 語言：地球
    "globe": circle(12, 12, 10) + line(2, 12, 22, 12) + arc(12, 12, 4.2, 0, 360, 72, ry=10),
    # 帳號：兩個人
    "users": (circle(9, 7, 4) + line(3, 21, 3, 19) + arc(7, 19, 4, 180, 270, 12) + line(7, 15, 11, 15)
              + arc(11, 19, 4, 270, 360, 12) + line(15, 19, 15, 21)
              + arc(15, 7, 4, -75.5, 75.5, 16) + line(21, 21, 21, 19) + arc(17, 19, 4, 0, -75, 12)),
    # 狀態：脈搏
    "pulse": poly((22, 12), (18, 12), (15, 21), (9, 3), (6, 12), (2, 12)),
    # 伺服器
    "server": (rounded_rect(2, 2, 22, 10, 2) + rounded_rect(2, 14, 22, 22, 2)
               + line(6, 6, 6.01, 6) + line(6, 18, 6.01, 18)),
    # 背景（風景）
    "landscape": rounded_rect(3, 3, 21, 21, 2) + circle(9, 9, 2) + poly((21, 15), (16, 10), (5, 21)),
    # 鍵盤（語音模式下按它回打字）
    "keyboard": (rounded_rect(2, 5, 22, 19, 2) + line(6, 9, 6.01, 9) + line(10, 9, 10.01, 9) + line(14, 9, 14.01, 9)
                 + line(18, 9, 18.01, 9) + line(8, 12.5, 8.01, 12.5) + line(12, 12.5, 12.01, 12.5)
                 + line(16, 12.5, 16.01, 12.5) + line(8, 15.5, 16, 15.5)),
    "home": poly((3, 10), (12, 3), (21, 10)) + poly((5, 9), (5, 21), (19, 21), (19, 9)) + poly((10, 21), (10, 15), (14, 15), (14, 21)),
    # 通話（2026-10-29 單人通話）：聽筒＝左上耳墊＋右下話筒墊，中間一道往左下鼓的弧
    "phone": rounded_rect(3, 2, 9, 8, 2) + rounded_rect(16, 15, 22, 21, 2) + arc(20, 4, 13, 90, 180, 24),
    # 掛斷：聽筒＋斜線
    "phone-off": (rounded_rect(3, 2, 9, 8, 2) + rounded_rect(16, 15, 22, 21, 2) + arc(20, 4, 13, 90, 180, 24)
                  + line(21, 3, 3, 21)),
    # 靜音：麥克風＋斜線
    "mic-off": (rounded_rect(9, 2, 15, 15, 3) + line(19, 10, 19, 11) + arc(12, 11, 7, 0, 180)
                + line(5, 11, 5, 10) + line(12, 18, 12, 22) + line(8, 22, 16, 22) + line(3, 3, 21, 21)),
}

#: 裝飾（實心或粗線；不是線描圖示）
DECOR = {
    # 光環：細圓＋上下左右四個小三角（原創幾何，取「光環」的語意）
    "halo": {"stroke": circle(12, 12, 8.5), "width": 1.1,
             "fill": [((12, 0.6), (13.3, 3.2), (10.7, 3.2)), ((12, 23.4), (10.7, 20.8), (13.3, 20.8)),
                      ((0.6, 12), (3.2, 10.7), (3.2, 13.3)), ((23.4, 12), (20.8, 13.3), (20.8, 10.7))]},
    # 三角碎片（點擊特效、角落裝飾）
    "tri": {"stroke": [], "width": 0, "fill": [((12, 3), (21.5, 20), (2.5, 20))]},
    # 四芒閃光
    "spark": {"stroke": [], "width": 0,
              "fill": [((12, 1), (13.6, 10.4), (12, 12), (10.4, 10.4)), ((23, 12), (13.6, 13.6), (12, 12), (13.6, 10.4)),
                       ((12, 23), (10.4, 13.6), (12, 12), (13.6, 13.6)), ((1, 12), (10.4, 10.4), (12, 12), (10.4, 13.6))]},
    # 點擊圈（細環）
    "ring": {"stroke": circle(12, 12, 10), "width": 1.4, "fill": []},
}


# ── 光柵化 ─────────────────────────────────────────────────────────────────

def _segment_distance(px, py, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    qx, qy = ax + t * dx - px, ay + t * dy - py
    return math.sqrt(qx * qx + qy * qy)


def _inside(px, py, polygon):
    inside = False
    count = len(polygon)
    for i in range(count):
        x0, y0 = polygon[i]
        x1, y1 = polygon[(i + 1) % count]
        if (y0 > py) != (y1 > py) and px < (x1 - x0) * (py - y0) / (y1 - y0) + x0:
            inside = not inside
    return inside


def rasterize(segments, width, fills=()):
    half = width / 2.0
    scale = GRID / SIZE
    alpha = bytearray(SIZE * SIZE)
    # 每個線段的外框（加速：只對附近的段算距離）
    boxes = [(min(a[0], b[0]) - half, max(a[0], b[0]) + half, min(a[1], b[1]) - half, max(a[1], b[1]) + half, a, b)
             for a, b in segments]
    for y in range(SIZE):
        for x in range(SIZE):
            hits = 0
            for sy in range(SUPERSAMPLE):
                for sx in range(SUPERSAMPLE):
                    px = (x + (sx + 0.5) / SUPERSAMPLE) * scale
                    py = (y + (sy + 0.5) / SUPERSAMPLE) * scale
                    covered = any(_inside(px, py, shape) for shape in fills)
                    if not covered:
                        for x0, x1, y0, y1, a, b in boxes:
                            if x0 <= px <= x1 and y0 <= py <= y1 and _segment_distance(px, py, a, b) <= half:
                                covered = True
                                break
                    hits += covered
            alpha[y * SIZE + x] = round(255 * hits / (SUPERSAMPLE * SUPERSAMPLE))
    return alpha


def write_png(path: pathlib.Path, alpha: bytearray):
    rows = bytearray()
    for y in range(SIZE):
        rows.append(0)
        for x in range(SIZE):
            rows += bytes((255, 255, 255, alpha[y * SIZE + x]))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b"")
    path.write_bytes(png)


def main(argv=None) -> int:
    only = set(argv or sys.argv[1:])
    OUT.mkdir(parents=True, exist_ok=True)
    for name, segments in ICONS.items():
        if only and name not in only:
            continue
        write_png(OUT / f"i-{name}.png", rasterize(segments, STROKE))
        print(f"✓ i-{name}.png")
    for name, spec in DECOR.items():
        if only and name not in only:
            continue
        write_png(OUT / f"i-{name}.png", rasterize(spec["stroke"], spec["width"], spec["fill"]))
        print(f"✓ i-{name}.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
