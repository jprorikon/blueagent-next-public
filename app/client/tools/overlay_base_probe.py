#!/usr/bin/env python3
"""**真相側「疊層底下是誰」的判準工具**：證明那幾屏是「底屏留著、只壓一層暗」。

為什麼要有這一支（2026-10-16）：台架先前把 `menu`／`settings`／`album`／`hand`／`confirm`／
`question` 當成**獨立一屏**在拍——而真相那幾屏是**遮罩層**（`.overlay-active`：底下那一屏
留著、蓋一層暗色、再放卡片）。判準不能靠「我覺得」，要靠像素：

    真相的疊層截圖，在**卡片以外的區域**是不是等於 `dim(底屏)`：
        dim(c) = (1−a)·c ＋ a·overlay色        （例如 `.menu-layer { background: rgba(18,39,66,.42) }`）

* median 殘差 ~1（且 >30 的點佔比個位數 %）⇒ 這個「底屏 ＋ 幾層暗」的假設成立
  （＝Unity 那一側也必須先建底屏，否則底下的東西整片不見）。
* 對不上 ⇒ 這一屏在真相是**不透明**的（例：`talk` 的面板是白底整片 ⇒ 不必底屏）。

實測（2026-10-12 的真值圖，`Builds/webui-*.png`；排除立繪區）：

| 疊層 | 底屏 | 暗層 | median／>30 |
|---|---|---|---|
| `menu`（四層都一樣） | `home` | `rgba(18,39,66,.42)` ×1 | 1.03／4.9% |
| `settings` | `home` | `rgba(23,47,74,.38)` ×1（`.app-shell::after`） | 0.7／~1% |
| `confirm/game`・`confirm/trpg` | `game`・`trpg` | `.42` ×1 | 1.26・0.53 |
| `question` | `home` | `.42` ×1 | 0.97 |
| `album` | `talk` | `rgba(23,47,74,.45)` ×1（`.album-sheet`） | 0.72 |
| `hand` | `settings`（再底下是 `home`） | `.42` ×1 | 1.12 |
| `talk/stickers` | —（面板不透明） | — | 對不上 ✓ 符合預期 |

用法（**用 `.venv-yolo` 的 python**，本機沒有 numpy／PIL）：

    .venv-yolo/bin/python tools/overlay_base_probe.py            # 跑上表那一組
    # 或改下面的 CASES 自己加一組
"""
import os

import numpy as np
from PIL import Image

BUILDS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Builds")

# (疊層真相檔, 底屏真相檔, 底標籤, 暗層 alpha, 暗層色, 卡片區（不比）)
CASES = [
    ("webui-menu", "webui-home", "home", 0.42, (18, 39, 66), (14, 230, 446, 620)),
    ("webui-menu-games", "webui-home", "home", 0.42, (18, 39, 66), (14, 230, 446, 620)),
    ("webui-menu-tables", "webui-home", "home", 0.42, (18, 39, 66), (14, 230, 446, 620)),
    ("webui-menu-seats", "webui-home", "home", 0.42, (18, 39, 66), (14, 230, 446, 620)),
    ("webui-settings-background", "webui-home", "home", 0.38, (23, 47, 74), (0, 105, 460, 536)),
    ("webui-settings-audio", "webui-home", "home", 0.38, (23, 47, 74), (0, 105, 460, 536)),
    ("webui-confirm-game", "webui-game", "game", 0.42, (18, 39, 66), (60, 300, 400, 520)),
    ("webui-confirm-trpg", "webui-trpg-paused", "trpg", 0.42, (18, 39, 66), (60, 300, 400, 520)),
    ("webui-question-zh-hant", "webui-home", "home", 0.42, (18, 39, 66), (60, 300, 400, 520)),
    ("webui-album-items", "webui-talk-chat", "talk", 0.45, (23, 47, 74), (10, 100, 450, 760)),
    ("webui-hand-locked_zh_hant", "webui-settings-service", "settings", 0.42, (18, 39, 66),
     (40, 250, 420, 600)),
    ("webui-talk-stickers", "webui-home", "（不透明，對不上才對）", 0.42, (18, 39, 66),
     (10, 100, 450, 760)),
]


def load(name: str) -> np.ndarray:
    return np.asarray(Image.open(os.path.join(BUILDS, name + ".png")).convert("RGB"), dtype=float)


def score(over: np.ndarray, base: np.ndarray, alpha: float, overlay: tuple, card: tuple):
    mask = np.zeros(over.shape[:2], dtype=bool)
    mask[::4, ::4] = True
    x0, y0, x1, y1 = card
    mask[y0:y1, x0:x1] = False
    expected = (1.0 - alpha) * base + alpha * np.asarray(overlay, dtype=float)
    delta = np.abs(over - expected).mean(axis=2)[mask]
    return float(np.median(delta)), float((delta > 30).mean() * 100)


def main() -> None:
    print(f"{'overlay':32s} {'base':10s} {'median':>9s} {'>30':>7s}")
    for over_name, base_name, label, alpha, overlay, card in CASES:
        try:
            over, base = load(over_name), load(base_name)
        except FileNotFoundError as exc:
            print(f"{over_name:32s} {label:10s}  （缺圖：{os.path.basename(exc.filename)}）")
            continue
        if over.shape != base.shape:
            print(f"{over_name:32s} {label:10s}  （尺寸不同）")
            continue
        median, ratio = score(over, base, alpha, overlay, card)
        verdict = "✅ 底屏＋暗層" if median < 3 else "✗ 對不上"
        print(f"{over_name:32s} {label:10s} {median:9.2f} {ratio:6.1f}%  {verdict}")


if __name__ == "__main__":
    main()
