"""延時聚合的送出內容（owner 2026-11-04：「對於用戶的前台文字／圖片／表情包發送，設計延時聚合發送」）。

App 在前台把連續幾筆（打字／照片／表情包）攢成**一輪**再送出，body 的形狀是：

    {"items": [
        {"kind": "text",    "text": "これ見て"},
        {"kind": "photo",   "media_id": "m_1730…"},
        {"kind": "sticker", "sticker": "smile"}
    ]}

這裡只做「形狀 → 一輪的輸入」的換算：
  · 文字按順序用換行接起來（她要看到的是他一次講的幾句）；
  · 表情包換成 prompt 認得的 `[[stamp_in:…]]` 標籤（與單張那條完全同一個格式）；
  · 照片依序排成 `media_list`（`VisionStage` 逐張看，見 `stages/intake.py`）；
  · `asked_parts`＝給客戶端歷史畫的「他自己發的那幾條」（與 `parts` 對稱）。

⚠️ 照片只認**同一個 principal 上傳的**（media store 的擁有者），別人的 id 一律 400。
"""

from __future__ import annotations

MIME_OF_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp"}


def _sticker(state, entity: str, sticker_id: str):
    catalog = getattr(getattr(state, "app", None), "stickers", None)
    if catalog is None or not sticker_id:
        return None
    try:
        return catalog.get(entity, sticker_id)
    except Exception:                                  # noqa: BLE001 - 目錄壞了當作沒有這張
        return None


def build_items(state, items, *, entity: str, principal: str) -> tuple[int, dict]:
    """把 `items` 換算成一輪的輸入；回 `(200, {...})` 或 `(4xx, {"error": …})`。"""
    if not isinstance(items, list) or not items:
        return 400, {"error": "items required"}
    texts: list[str] = []
    tags: list[str] = []
    media_list: list[dict] = []
    asked_parts: list[dict] = []
    stickers: list[str] = []
    store = getattr(state, "media", None)
    for raw in items:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "text").strip().lower()
        text = str(raw.get("text") or "").strip()
        if kind == "photo":
            media_id = str(raw.get("media_id") or raw.get("id") or "").strip()
            row = store.row_of(media_id) if (store is not None and media_id) else None
            path = store.path_of(media_id) if (store is not None and media_id) else None
            if row is None or path is None or str(row.get("principal") or "") != str(principal):
                return 400, {"error": f"unknown media: {media_id or '-'}"}
            ext = str(row.get("ext") or ".png").lower()
            media_list.append({"id": media_id, "ext": ext, "bytes": row.get("bytes"),
                               "mime": MIME_OF_EXT.get(ext, "image/png"), "path": str(path)})
            asked_parts.append({"kind": "photo", "media_id": media_id, "text": text})
            if text:
                texts.append(text)
            continue
        if kind == "sticker":
            sticker_id = str(raw.get("sticker") or "").strip()
            found = _sticker(state, entity, sticker_id)
            if found is None:
                return 400, {"error": "unknown sticker"}
            tags.append(f"[[stamp_in:{found.meaning}]]")
            stickers.append(found.id)
            asked_parts.append({"kind": "sticker", "sticker": found.id, "text": text})
            if text:
                texts.append(text)
            continue
        if text:
            texts.append(text)
            asked_parts.append({"kind": "text", "text": text})
    if not texts and not tags and not media_list:
        return 400, {"error": "text required"}
    joined = "\n".join(texts).strip()
    prompt_text = " ".join(tags + ([joined] if joined else [])).strip()
    asked = "\n".join(part["text"] for part in asked_parts if part.get("text")).strip()
    return 200, {"text": prompt_text, "asked": asked, "asked_parts": asked_parts,
                 "media_list": media_list, "stickers": stickers}


__all__ = ["build_items", "MIME_OF_EXT"]
