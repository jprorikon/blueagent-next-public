"""App Server 自有图片库：SQLite 元数据、私有二进制、内容授权与缩图。"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time

from app.server.social.models import Actor, BoardError

MAX_BYTES = 6 * 1024 * 1024
THUMB_WIDTHS = (192, 360)
_CONTENT_TYPES = {
    "image/png": (".png", "image/png"),
    "image/jpeg": (".jpg", "image/jpeg"),
    "image/webp": (".webp", "image/webp"),
}
_MEDIA_ID = re.compile(r"m[0-9a-f]{8,64}")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_media (
  id TEXT PRIMARY KEY, ext TEXT NOT NULL, mime TEXT NOT NULL,
  bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, at REAL NOT NULL,
  owner_id TEXT NOT NULL, entity TEXT NOT NULL DEFAULT '',
  turn_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS app_media_owner ON app_media(owner_id, at DESC, id DESC);
"""


def _matches_etag(header: str, tag: str) -> bool:
    for candidate in str(header or "").split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate.strip('"') == tag:
            return True
    return False


class MediaService:
    """平台媒体边界；不 import Agent，也不把服务器路径交给调用者。"""

    def __init__(self, db_path, data_root, *, board=None, chat_store=None,
                 clock=time.time) -> None:
        self.root = pathlib.Path(data_root) / "media"
        self.root.mkdir(parents=True, exist_ok=True)
        self.thumb_root = pathlib.Path(data_root) / "thumbs"
        self.db_path = str(db_path)
        self.board = board
        self.chat_store = chat_store
        self.clock = clock
        self._lock = threading.RLock()
        self._thumb_lock = threading.Lock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _public(row: sqlite3.Row | dict) -> dict:
        keys = ("id", "ext", "mime", "bytes", "sha256", "at", "owner_id", "name")
        result = {key: row[key] for key in keys}
        result["bytes"] = int(result["bytes"])
        result["at"] = float(result["at"])
        result["url"] = "/v1/media/" + str(result["id"])
        return result

    def owner(self, media_id: str) -> str | None:
        if not _MEDIA_ID.fullmatch(str(media_id or "")):
            return None
        with self._lock:
            row = self._db.execute("SELECT owner_id FROM app_media WHERE id=?",
                                   (str(media_id),)).fetchone()
        return str(row["owner_id"]) if row is not None else None

    def upload(self, actor: Actor, data: bytes, content_type: str, *, name: str = "") -> dict:
        ext, mime = _CONTENT_TYPES.get(str(content_type or "").split(";")[0].strip().lower(),
                                      ("", ""))
        if not ext:
            raise BoardError(415, "unsupported_type")
        if not data:
            raise BoardError(400, "image_required")
        if len(data) > MAX_BYTES:
            raise BoardError(413, "image_too_large", limit=MAX_BYTES)
        media_id = "m" + secrets.token_hex(8)
        return self._save(media_id, data, ext=ext, mime=mime, owner_id=actor.id,
                          at=float(self.clock()), name=name)

    def _save(self, media_id: str, data: bytes, *, ext: str, mime: str,
              owner_id: str, at: float, entity: str = "", turn_id: str = "",
              name: str = "") -> dict:
        if not _MEDIA_ID.fullmatch(media_id):
            raise ValueError("bad_media_id")
        if not data or len(data) > MAX_BYTES:
            raise ValueError("bad_media_size")
        digest = hashlib.sha256(data).hexdigest()
        target = self.root / (media_id + ext)
        temporary = self.root / ("." + media_id + "." + secrets.token_hex(6) + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            with self._lock, self._db:
                existing = self._db.execute("SELECT * FROM app_media WHERE id=?",
                                            (media_id,)).fetchone()
                if existing is not None:
                    if existing["sha256"] != digest or existing["owner_id"] != owner_id:
                        raise BoardError(409, "media_conflict")
                    os.replace(temporary, target)
                    return self._public(existing)
                os.replace(temporary, target)
                try:
                    self._db.execute(
                        "INSERT INTO app_media VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (media_id, ext, mime, len(data), digest, float(at), str(owner_id),
                         str(entity or ""), str(turn_id or ""), str(name or "")))
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
        finally:
            temporary.unlink(missing_ok=True)
        with self._lock:
            row = self._db.execute("SELECT * FROM app_media WHERE id=?", (media_id,)).fetchone()
        return self._public(row)

    def import_legacy(self, row: dict, data: bytes, owner_id: str) -> dict:
        """保留旧 ID／时间／正文；同内容重放幂等，不同内容或归属绝不覆盖。"""
        ext = str(row.get("ext") or "").lower()
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp"}.get(ext, "")
        if not mime:
            raise ValueError("unsupported_type")
        return self._save(str(row.get("id") or ""), data, ext=ext, mime=mime,
                          owner_id=str(owner_id), at=float(row.get("at") or 0),
                          entity=str(row.get("entity") or ""), turn_id=str(row.get("turn_id") or ""),
                          name=str(row.get("name") or ""))

    def list(self, actor: Actor, *, offset: int = 0, limit: int = 30) -> dict:
        offset = max(0, int(offset or 0))
        limit = max(1, min(60, int(limit or 30)))
        with self._lock:
            total = self._db.execute("SELECT COUNT(*) AS n FROM app_media WHERE owner_id=?",
                                     (actor.id,)).fetchone()["n"]
            rows = self._db.execute(
                "SELECT * FROM app_media WHERE owner_id=? ORDER BY at DESC,id DESC LIMIT ? OFFSET ?",
                (actor.id, limit, offset)).fetchall()
        return {"offset": offset, "limit": limit, "total": int(total),
                "items": [self._public(row) for row in rows], "url_prefix": "/v1/media/"}

    def _row(self, media_id: str) -> sqlite3.Row | None:
        if not _MEDIA_ID.fullmatch(str(media_id or "")):
            return None
        with self._lock:
            return self._db.execute("SELECT * FROM app_media WHERE id=?", (media_id,)).fetchone()

    def can_read(self, actor: Actor, row: sqlite3.Row | dict) -> bool:
        if row["owner_id"] == actor.id:
            return True
        media_id = str(row["id"])
        if self.chat_store is not None and self.chat_store.can_read_media(actor.id, media_id):
            return True
        return bool(self.board is not None and self.board.can_read_media(
            actor, media_id, str(row["owner_id"])))

    def purge(self, media_id: str, owner_id: str) -> bool:
        """刪掉一張**沒人再引用**的圖（列與檔案）。不是 `owner_id` 上傳的、或還有引用 ⇒ 不動，回 False。

        「引用」＝任何一則私聊／群聊訊息，或任何一則公共區帖（**含已刪的帖**：帖刪除可能還要復原，保守起見不碰）。
        縮圖快取不必刪：讀圖要先過 `app_media` 這一列，列沒了就再也讀不到。
        """
        row = self._row(media_id)
        if row is None or str(row["owner_id"]) != str(owner_id):
            return False
        if self.chat_store is not None and self.chat_store.media_referenced(str(media_id)):
            return False
        if self.board is not None and self.board.references_media(str(media_id)):
            return False
        with self._lock, self._db:
            self._db.execute("DELETE FROM app_media WHERE id=?", (str(media_id),))
        (self.root / (str(row["id"]) + str(row["ext"]))).unlink(missing_ok=True)
        return True

    def metadata(self, actor: Actor, media_id: str) -> dict:
        row = self._row(media_id)
        if row is None or not self.can_read(actor, row):
            raise BoardError(404, "not_found")
        return self._public(row)

    def read(self, actor: Actor, media_id: str, *, width: int = 0) -> tuple[dict, bytes, str, str]:
        row = self._row(media_id)
        if row is None or not self.can_read(actor, row):
            raise BoardError(404, "not_found")
        target = self.root / (str(row["id"]) + str(row["ext"]))
        try:
            data = target.read_bytes()
        except OSError as exc:
            raise BoardError(404, "not_found") from exc
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise BoardError(404, "not_found")
        tag = str(row["sha256"])
        mime = str(row["mime"])
        if width in THUMB_WIDTHS:
            tag += f"-w{width}"
            small = thumbnail(target, self.thumb_root, width, self._thumb_lock)
            if small is not None:
                data = small.read_bytes()
                mime = "image/png" if small.suffix == ".png" else "image/jpeg"
        return self._public(row), data, mime, tag


def thumbnail(source: pathlib.Path, thumb_root: pathlib.Path, width: int,
              lock: threading.Lock) -> pathlib.Path | None:
    """媒体与贴纸共用的磁盘缩图；无 ffmpeg 或转换失败时返回原图。"""
    ffmpeg = shutil.which(os.environ.get("BLUEAGENT_FFMPEG") or "ffmpeg")
    if not ffmpeg:
        return None
    try:
        stat = source.stat()
    except OSError:
        return None
    png = source.suffix.lower() == ".png"
    key = hashlib.sha256(
        f"{source.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{width}".encode()).hexdigest()[:24]
    target = thumb_root / f"{key}.{'png' if png else 'jpg'}"
    if target.is_file():
        return target
    thumb_root.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.stem + f".{threading.get_ident()}.tmp{target.suffix}")
    command = [ffmpeg, "-v", "error", "-y", "-i", str(source),
               "-vf", f"scale='min({width},iw)':-2", "-frames:v", "1"]
    command += ["-compression_level", "9"] if png else ["-q:v", "4"]
    command.append(str(temporary))
    try:
        with lock:
            if target.is_file():
                return target
            subprocess.run(command, check=True, timeout=20,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            temporary.replace(target)
    except (OSError, subprocess.SubprocessError):
        temporary.unlink(missing_ok=True)
        return None
    return target if target.is_file() else None


__all__ = ["MAX_BYTES", "MediaService", "THUMB_WIDTHS", "_matches_etag", "thumbnail"]
