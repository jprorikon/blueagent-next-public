"""內容雜湊（owner 2026-10-29：「圖片、表情包和聊天記錄採用 app 側快取，對比線上的雜湊決定要不要重新同步」）。

端末把每份東西連同這個雜湊一起存在本機；下次要用時：
  - 清單（表情包、聊天記錄）帶 `If-None-Match: "<雜湊>"` 來問 ⇒ 沒變就回 304，什麼都不用重傳；
  - 單張圖：清單裡已經附了每張的雜湊 ⇒ 跟本機那份一樣就**連問都不用問**。

雜湊取 sha256 的前 16 個十六進位字（64 bit）：只用來判斷「同一個網址的內容變了沒有」，不是拿來防竄改，
這個長度撞到的機率可以忽略。
"""

from __future__ import annotations

import hashlib
import json

#: 雜湊長度（十六進位字數）
LENGTH = 16


def content_tag(data: bytes) -> str:
    """一段位元組的雜湊。"""
    return hashlib.sha256(bytes(data or b"")).hexdigest()[:LENGTH]


def json_tag(value) -> str:
    """一份 JSON 資料的雜湊（鍵排序後算 ⇒ 字典順序不同也算同一份）。"""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return content_tag(text.encode("utf-8"))


def matches(if_none_match: str, tag: str) -> bool:
    """`If-None-Match` 標頭裡有沒有這個雜湊（可能是逗號分隔的多個、可能帶 `W/`、可能是 `*`）。"""
    if not tag or not if_none_match:
        return False
    for part in str(if_none_match).split(","):
        part = part.strip()
        if part == "*":
            return True
        if part.startswith("W/"):
            part = part[2:]
        if part.strip('"') == tag:
            return True
    return False


__all__ = ["LENGTH", "content_tag", "json_tag", "matches"]
