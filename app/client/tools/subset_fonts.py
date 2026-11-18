#!/usr/bin/env python3
"""把 Noto Sans JP／SC／TC 裁成常用字子集（2026-10-31：三支全字型 23.5 MB 全進包，裁完 7.6 MB）。

缺的字不會變豆腐：`NextFonts` 的 fallback 鏈退回 `Fonts/DroidSansFallback`（全字集）。

    # 需要 fontTools（只在出字型時要，App 與後端都不依賴）：pip install fonttools
    python3 app/client/tools/subset_fonts.py --src <放原始 NotoSans{JP,SC,TC}-Regular.ttf 的資料夾>

字集：
- JP：JIS X 0208（常用日文，約 6,900 字）
- SC：GB2312（約 7,400 字）
- TC：Big5 常用字（A440–C67E，5,401 字）
- 三支都加：ASCII、Latin-1、標點、箭頭、框線、假名、全形字、`Resources/i18n/*.json` 裡出現的每一個字
原始字型：google/fonts（OFL；可變字型切出 wght=400 的靜態實例）。授權文 `Resources/Fonts/NotoSansCJK-OFL.txt`。
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
FONTS = ROOT / "BlueAgentUnity" / "Assets" / "Resources" / "Fonts"
I18N = ROOT / "BlueAgentUnity" / "Assets" / "Resources" / "i18n"

RANGES = [(0x20, 0x7E), (0xA0, 0xFF), (0x2000, 0x206F), (0x2190, 0x21FF), (0x2460, 0x24FF),
          (0x2500, 0x257F), (0x25A0, 0x26FF), (0x3000, 0x303F), (0x3040, 0x30FF), (0x31F0, 0x31FF),
          (0xFF00, 0xFFEF)]


def _two_byte(codec: str, lead, trail) -> set[str]:
    out = set()
    for a in lead:
        for b in trail:
            try:
                out.add(bytes([a, b]).decode(codec))
            except UnicodeDecodeError:
                pass
    return out


def charsets() -> dict[str, set[str]]:
    common = {chr(c) for lo, hi in RANGES for c in range(lo, hi + 1)}
    for path in I18N.glob("*.json"):
        common |= set(path.read_text(encoding="utf-8"))
    jis = _two_byte("euc_jp", range(0xA1, 0xFF), range(0xA1, 0xFF))
    gb = _two_byte("gb2312", range(0xA1, 0xF8), range(0xA1, 0xFF))
    big5 = set()
    for a in range(0xA1, 0xC7):
        for b in list(range(0x40, 0x7F)) + list(range(0xA1, 0xFF)):
            if a >= 0xA4 and not 0xA440 <= (a << 8 | b) <= 0xC67E:
                continue
            try:
                big5.add(bytes([a, b]).decode("big5"))
            except UnicodeDecodeError:
                pass
    # ⚠️ 空白要留：`isprintable()` 把全形空白 U+3000 當不可印 ⇒ 第一版子集裡沒有它（她的日語常用全形空白）
    keep = lambda chars: {c for c in chars if c.isprintable() or c.isspace()}  # noqa: E731
    return {"JP": keep(jis | common), "SC": keep(gb | common), "TC": keep(big5 | common)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--src", type=pathlib.Path, required=True, help="原始 NotoSans{JP,SC,TC}-Regular.ttf 所在資料夾")
    args = parser.parse_args()
    for name, chars in charsets().items():
        source = args.src / f"NotoSans{name}-Regular.ttf"
        target = FONTS / f"NotoSans{name}-Regular.ttf"
        with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as text:
            text.write("".join(sorted(chars)))
        subprocess.run([sys.executable, "-m", "fontTools.subset", str(source), f"--text-file={text.name}",
                        f"--output-file={target}", "--layout-features=*", "--no-hinting", "--desubroutinize"],
                       check=True)
        print(f"✓ {target.name}：{len(chars)} 字，{target.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
