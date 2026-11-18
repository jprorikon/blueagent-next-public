"""可快取的 JSON／音檔 HTTP 表示；由 `server.http.Handler` 的小包裝呼叫。"""

from __future__ import annotations

import gzip
import json
import mimetypes
import pathlib

from agent.brain.media import digest
from agent.server.http_encoding import accepts_gzip, byte_range


_MIME = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".svg": "image/svg+xml",
}


def mime_for(path: pathlib.Path) -> str:
    return _MIME.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] \
        or "application/octet-stream"


def json_response(handler, status: int, payload: dict, *, etag: str = "") -> None:
    """JSON 大於 2 KB 可按協商 gzip；只有壓縮表示使用弱 ETag。"""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    compressed = len(raw) > 2048 and accepts_gzip(handler.headers.get("Accept-Encoding", ""))
    data = gzip.compress(raw, compresslevel=6, mtime=0) if compressed else raw
    if etag and handler._not_modified(etag, weak=compressed, vary=True):
        return
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Vary", "Accept-Encoding")
    if compressed:
        handler.send_header("Content-Encoding", "gzip")
    if etag:
        handler.send_header("ETag", f'{"W/" if compressed else ""}"{etag}"')
        handler.send_header("Cache-Control", "private, no-cache")
    else:
        handler.send_header("Cache-Control", "no-store")
    if status == 401:
        handler.send_header("WWW-Authenticate", "Bearer")
    elif status == 429:
        handler.send_header("Retry-After", "60")
    handler.end_headers()
    handler.wfile.write(data)


def turn_audio(handler, audio: bytes) -> None:
    """補拿音檔：內容快取＋單一段 Range，斷線不必從頭重傳。"""
    tag = digest.content_tag(audio)
    if handler._not_modified(tag):
        return
    selected = byte_range(handler.headers.get("Range", ""), len(audio))
    if selected is False:
        handler.send_response(416)
        handler.send_header("Content-Range", f"bytes */{len(audio)}")
        handler.send_header("Content-Length", "0")
        handler.send_header("Accept-Ranges", "bytes")
        handler.end_headers()
        return
    partial = selected is not None
    start, end = selected if partial else (0, len(audio) - 1)
    body = audio[start:end + 1]
    handler.send_response(206 if partial else 200)
    handler.send_header("Content-Type", "audio/wav")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("ETag", f'"{tag}"')
    handler.send_header("Cache-Control", "private, no-cache")
    handler.send_header("Accept-Ranges", "bytes")
    if partial:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{len(audio)}")
    handler.end_headers()
    handler.wfile.write(body)


def file_response(handler, path: pathlib.Path) -> None:
    """靜態檔回應；HTML／JS／CSS 不缓存，其他資源按小時缓存。"""
    if not path.is_file():
        handler._json(404, {"error": "not found"})
        return
    data = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", mime_for(path))
    handler.send_header("Content-Length", str(len(data)))
    cache = "no-store" if path.suffix.lower() in {".html", ".js", ".css"} \
        else "public, max-age=3600"
    handler.send_header("Cache-Control", cache)
    handler.end_headers()
    handler.wfile.write(data)


__all__ = ["file_response", "json_response", "mime_for", "turn_audio"]
