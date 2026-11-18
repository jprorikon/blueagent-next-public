"""圖片輸入與媒體庫：/api/image、/api/media。"""

from __future__ import annotations

import urllib.parse
from agent.brain.compose.state import AppState
from agent.brain.domain.auth import AuthContext, Role

from agent.server.api.turns import prepare_send

#: 訪客能不能送圖（含語音）：**能**（owner 2026-11-02「让 guest 可以发图片和语音信息，只是不允许通话」）。
#: 從前這裡寫的是 `getattr(auth, "can_send", False)`，而 `AuthContext` 從來沒有這個欄位
#: ⇒ `hasattr=False` ⇒ 判式恆真 ⇒ **guest 送照片一律 403**（App 的「写真」鈕對 guest 沒隱藏，
#: 按了就被拒，看起來像壞掉）。權限本來就由 `prepare_send` 的 `can("chat")` 統一管；
#: 通話才是不給 guest 的那一項，而它在 `/api/voice/*`（owner-only）與 `/api/audio` 的 `X-Call`
#: （guest ⇒ 403）兩處各自擋住，不需要在這裡重複。


def prepare_image(state: AppState, data: bytes, *, headers: dict,
                  auth: AuthContext | None = None, ip: str = "",
                  source: str = "", external_id: str = "") -> tuple[int, dict]:
    """`POST /api/image`：body 是圖檔（png/jpg/webp），回應走 SSE（第一幀 `image`）。

    與 `/api/audio` 同一個形狀：存檔 → 一輪 turn（kind=image ⇒ 管線 `media.see`，
    先由本地 VL 讀成文字，再走打字那條）。圖本身不進腦的上下文。
    """
    principal = getattr(auth, "principal", "") or ""
    if not principal:
        return 401, {"error": "unauthorized"}
    store = getattr(state, "media", None)
    if store is None:
        return 503, {"error": "media_unavailable"}
    if not data:
        return 400, {"error": "image required"}
    if len(data) > store_max_bytes():
        return 413, {"error": "image_too_large", "limit": store_max_bytes()}
    ctype = str(headers.get("Content-Type") or "").split(";")[0].strip().lower()
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(ctype, "")
    if not ext:
        return 415, {"error": "unsupported_type", "content_type": ctype}
    entity = str(headers.get("X-Entity") or "").strip()
    # ⚠️ HTTP header 是 latin-1：非 ASCII（日文/中文）要在客戶端 EscapeURL、這裡 unquote
    raw_note = str(headers.get("X-Note") or "").strip()
    note = urllib.parse.unquote(raw_note) if raw_note else ""
    try:
        row = store.save(data, ext=ext, principal=principal, entity=entity,
                         name=str(headers.get("X-Name") or ""))
    except ValueError as exc:
        return 400, {"error": str(exc)}
    body = {"text": note, "entity": entity,
            "mode": str(headers.get("X-Mode") or ""),
            "request_id": str(headers.get("X-Request-Id") or ""),
            "want_voice": str(headers.get("X-Want-Voice") or "").strip().lower() in {"1", "true", "yes", "on"},
            "source": "api:image",
            "kind": "image",
            "media": {"id": row["id"], "ext": row["ext"], "bytes": row["bytes"],
                      "mime": ctype,
                      "path": str(store.path_of(row["id"]) or "")}}
    status, data_out = prepare_send(state, body, auth=auth, ip=ip, source=source,
                                    external_id=external_id, trusted_source="api:image")
    if status != 200:
        return status, data_out
    # ⚠️ `data_out["media"]` 是**給管線用的**（含 `path`／`mime`：VisionStage 要靠 path 讀圖）。
    #    從前這一行把它換成「只給端末看的那幾個欄位」，於是 `/api/image` 送進 `send_stream` 的 media
    #    沒有 path ⇒ VisionStage 連 VL 都沒叫，直接寫「（読めませんでした）」，看起來就是她看不到圖
    #    （2026-10-27 實測：pipeline=media.see 但 desc=0、圖檔根本沒被讀）。
    #    端末要的那份另開一個鍵（`media_public`），由 `server/http.py` 當 SSE 首幀回。
    data_out["media_public"] = {k: row[k] for k in ("id", "ext", "bytes", "at")}
    data_out["input"] = "image"
    return status, data_out


def store_max_bytes() -> int:
    from agent.brain.media.store import MAX_BYTES
    return int(MAX_BYTES)


def store_upload(state: AppState, data: bytes, *, headers: dict,
                 auth: AuthContext | None = None) -> tuple[int, dict]:
    """`POST /api/media`：**只存圖、不起回合**。

    為什麼要這支（owner 2026-11-04「延時聚合發送」）：App 前台會先把連發的照片上傳拿 id，
    等視窗關閉時再一次把「文字＋照片＋表情」合成一輪送出；`/api/image` 是「存圖＋立刻跑一輪」，
    不合這個用法。
    """
    principal = getattr(auth, "principal", "") or ""
    if not principal:
        return 401, {"error": "unauthorized"}
    store = getattr(state, "media", None)
    if store is None:
        return 503, {"error": "media_unavailable"}
    if not data:
        return 400, {"error": "image required"}
    if len(data) > store_max_bytes():
        return 413, {"error": "image_too_large", "limit": store_max_bytes()}
    ctype = str(headers.get("Content-Type") or "").split(";")[0].strip().lower()
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(ctype, "")
    if not ext:
        return 415, {"error": "unsupported_type", "content_type": ctype}
    entity = str(headers.get("X-Entity") or "").strip()
    try:
        row = store.save(data, ext=ext, principal=principal, entity=entity,
                         name=str(headers.get("X-Name") or ""))
    except ValueError as exc:
        return 400, {"error": str(exc)}
    return 200, {"id": row["id"], "ext": row["ext"], "bytes": row["bytes"],
                 "at": row.get("at"), "url": "/api/media/" + str(row["id"])}


def upload_or_prepare_image(state: AppState, data: bytes, *, headers: dict,
                            auth: AuthContext | None = None, ip: str = "",
                            source: str = "", external_id: str = "",
                            store_only: bool = False) -> tuple[int, dict]:
    """`/api/image` 與 `/api/media` 共用的單一入口：只存圖（`store_only`）或存圖＋跑一輪。

    為什麼要分這兩種（owner 2026-11-04 的「延時聚合發送」）：`/api/image` 是「存圖＋立刻跑一輪」，
    而聚合要把連發的照片先換成 `media_id`、等視窗關閉才一次送出 ⇒ 需要一個只存不跑的入口。
    """
    if store_only:
        return store_upload(state, data, headers=headers, auth=auth)
    return prepare_image(state, data, headers=headers, auth=auth, ip=ip,
                         source=source, external_id=external_id)


def list_media(state: AppState, query: dict, *, auth: AuthContext | None = None) -> tuple[int, dict]:
    """`GET /api/media?offset=&limit=`：相册（自己的那些，新的在前）。"""
    principal = getattr(auth, "principal", "") or ""
    if not principal:
        return 401, {"error": "unauthorized"}
    store = getattr(state, "media", None)
    if store is None:
        return 503, {"error": "media_unavailable"}
    try:
        offset = max(0, int((query.get("offset") or ["0"])[0]))
        limit = max(1, min(60, int((query.get("limit") or ["30"])[0])))
    except (TypeError, ValueError):
        offset, limit = 0, 30
    page = store.list(offset=offset, limit=limit, principal=principal)
    page["url_prefix"] = "/api/media/"
    return 200, page
