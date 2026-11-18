"""縮圖（owner 2026-10-30「網路傳輸優化」）：相冊牆、表情盤不必抓原圖。

  - 相冊牆一頁 30 格，以前每格都抓原圖（JPEG，長邊到 2048）⇒ 第一次打開十幾 MB；
  - 表情盤一格 72 設計單位，256 px 的原圖用不到 ⇒ 104 張第一次就是 9.5 MB。

用 ffmpeg 縮（image 裡本來就有，語音那條路要它；不另外裝 Pillow）。縮好的放快取目錄，
key＝原檔路徑＋mtime＋大小＋寬 ⇒ 原檔換了自然重做。ffmpeg 不在或失敗 ⇒ 回 None，呼叫端就給原圖（照樣能用）。

ETag：`<原圖雜湊>-w<寬>`（見 `thumb_tag`）——端末手上的清單有原圖雜湊，自己就推得出縮圖的雜湊，不必先問。
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import subprocess
import threading

#: 只收這幾個寬（免得亂給的 `?w=` 把快取撐爆）：相冊格、表情盤格
WIDTHS = (192, 360)

_lock = threading.Lock()


def pick_width(raw) -> int:
    """`?w=` ⇒ 允許的寬；不認得就 0（＝原圖）。"""
    try:
        width = int(str(raw or "0").strip())
    except ValueError:
        return 0
    return width if width in WIDTHS else 0


def cache_dir() -> pathlib.Path:
    """縮圖放哪（資料目錄底下；跟 `server/main.py` 的 media 同一個根）。"""
    return pathlib.Path(os.environ.get("BLUEAGENT_DATA_DIR") or "data") / "thumbs"


def thumb_tag(original_tag: str, width: int) -> str:
    return f"{original_tag}-w{width}" if original_tag and width else original_tag


def thumbnail(source: pathlib.Path, width: int, cache_dir: pathlib.Path) -> pathlib.Path | None:
    """`source` 縮成寬 `width`（比原圖小才縮；PNG 留透明，其餘出 JPEG）；做不出來回 None。"""
    if width not in WIDTHS:
        return None
    try:
        stat = source.stat()
    except OSError:
        return None
    png = source.suffix.lower() == ".png"
    key = hashlib.sha256(f"{source.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{width}".encode()).hexdigest()[:24]
    target = pathlib.Path(cache_dir) / f"{key}.{'png' if png else 'jpg'}"
    if target.is_file():
        return target
    ffmpeg = shutil.which(os.environ.get("BLUEAGENT_FFMPEG") or "ffmpeg")
    if not ffmpeg:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.stem + f".{threading.get_ident()}.tmp{target.suffix}")
    # 只縮不放大；高度跟著比例（-2：偶數，編碼器要求）
    command = [ffmpeg, "-v", "error", "-y", "-i", str(source),
               "-vf", f"scale='min({width},iw)':-2", "-frames:v", "1"]
    command += ["-compression_level", "9"] if png else ["-q:v", "4"]
    command.append(str(temp))
    try:
        with _lock:                                   # 同時好幾格要縮：一次一個，NAS 的 CPU 不被吃滿
            if target.is_file():
                return target
            subprocess.run(command, check=True, timeout=20,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            temp.replace(target)
    except (OSError, subprocess.SubprocessError):
        try:
            temp.unlink()
        except OSError:
            pass
        return None
    return target if target.is_file() else None


__all__ = ["WIDTHS", "cache_dir", "pick_width", "thumb_tag", "thumbnail"]
