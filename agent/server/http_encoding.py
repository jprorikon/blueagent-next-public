"""HTTP 內容協商的小函式（保持 Handler 只負責寫回應）。"""

from __future__ import annotations


def accepts_gzip(header: str) -> bool:
    """客戶端是否接受 gzip；明確 `gzip;q=0` 或 `*;q=0` 要尊重。"""
    wildcard = False
    for item in str(header or "").split(","):
        parts = [part.strip() for part in item.split(";")]
        coding = parts[0].lower()
        if coding not in {"gzip", "*"}:
            continue
        try:
            quality = float(next((part.split("=", 1)[1] for part in parts[1:]
                                  if part.lower().startswith("q=")), "1"))
        except (TypeError, ValueError):
            quality = 0.0
        if coding == "gzip":
            return quality > 0.0
        wildcard = quality > 0.0
    return wildcard


def byte_range(header: str, size: int):
    """單一 HTTP bytes range → `(start, end)`；無標頭回 None，無效回 `False`。

    多重 range 不支援（當成無效，避免端末把 multipart 當 WAV）；suffix 與開放結尾
    都依 RFC 9110 截到實際檔案大小。
    """
    raw = str(header or "").strip()
    if not raw:
        return None
    if not raw.startswith("bytes=") or "," in raw or size <= 0:
        return False
    spec = raw[6:].strip()
    start_text, separator, end_text = spec.partition("-")
    if not separator or (not start_text and not end_text):
        return False
    try:
        if not start_text:                         # 最後 N bytes
            length = int(end_text)
            if length <= 0:
                return False
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
    except ValueError:
        return False
    if start < 0 or end < start or start >= size:
        return False
    return start, min(end, size - 1)


__all__ = ["accepts_gzip", "byte_range"]
