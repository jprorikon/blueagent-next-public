"""客户端贴纸目录：每角色一包，共用包在 ``_shared/``。

素材唯一真源是 ``app/client/assets/stickers/``。``stickers.json`` 为每张图提供
稳定 ID、显示名和给 Agent 的语义 ``meaning``；没有清单时按图片文件名生成。
只收 PNG／JPEG，避免 Unity 运行期无法解码。每次读取检查 mtime，换素材不必重启。
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import threading
from dataclasses import dataclass

#: 图档后缀 → MIME（HTTP 层也使用同一张表）。
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
SHARED = "_shared"
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def content_tag(data: bytes) -> str:
    """与旧客户端缓存契约一致的 64 bit 内容标签。"""
    return hashlib.sha256(bytes(data or b"")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class Sticker:
    id: str
    entity: str
    file: str
    label: str
    meaning: str

    def to_dict(self, *, url_prefix: str = "/api/stickers") -> dict:
        """返回语义与内容 URL；绝不暴露服务器文件路径。"""
        prefix = "/" + str(url_prefix or "/api/stickers").strip("/")
        return {"id": self.id, "entity": self.entity, "label": self.label,
                "url": f"{prefix}/{self.entity}/{self.id}"}


class StickerCatalog:
    def __init__(self, root: pathlib.Path | str) -> None:
        self.root = pathlib.Path(root)
        self._lock = threading.Lock()
        #: entity → (簽名, 清單)；簽名＝目錄裡各檔的 mtime，變了就重讀
        self._cache: dict[str, tuple[tuple, list[Sticker]]] = {}
        #: 圖檔路徑 → ((mtime, 大小), 雜湊)：每次列清單都要附每張的雜湊，不能每次都重讀 104 張圖
        self._hashes: dict[str, tuple[tuple, str]] = {}

    def list(self, entity: str) -> list[Sticker]:
        """她能用的那幾張：自己那包在前，共用包接在後（同名 id 以自己那包為準）。"""
        name = str(entity or "").strip().lower()
        if not _ID.match(name):
            return []
        own = self._pack(self.root / name)
        taken = {item.id for item in own}
        shared = [Sticker(item.id, name, item.file, item.label, item.meaning)
                  for item in self._pack(self.root / SHARED) if item.id not in taken]
        return own + shared

    def get(self, entity: str, sticker_id: str) -> Sticker | None:
        wanted = str(sticker_id or "").strip().lower()
        return next((item for item in self.list(entity) if item.id == wanted), None)

    def path_of(self, sticker: Sticker) -> pathlib.Path | None:
        own = self._pack(self.root / sticker.entity)
        folder = self.root / (sticker.entity if any(item.id == sticker.id for item in own)
                              else SHARED)
        path = (folder / sticker.file).resolve()
        # 清單裡的 `file` 是人寫的：`../` 之類一律不認（只准目錄裡的檔）
        if path.parent != folder.resolve() or not path.is_file():
            return None
        return path

    def hash_of(self, sticker: Sticker) -> str:
        """這張圖的內容雜湊（端末拿來跟本機快取比：一樣就不必重抓）；圖不見了回空字串。"""
        path = self.path_of(sticker)
        if path is None:
            return ""
        try:
            stat = path.stat()
        except OSError:
            return ""
        stamp = (stat.st_mtime_ns, stat.st_size)
        key = str(path)
        with self._lock:
            cached = self._hashes.get(key)
            if cached is not None and cached[0] == stamp:
                return cached[1]
        try:
            tag = content_tag(path.read_bytes())
        except OSError:
            return ""
        with self._lock:
            self._hashes[key] = (stamp, tag)
        return tag

    # ── 內部 ────────────────────────────────────────────────────────────────
    def _pack(self, folder: pathlib.Path) -> list[Sticker]:
        if not folder.is_dir():
            return []
        signature = self._signature(folder)
        with self._lock:
            cached = self._cache.get(folder.name)
            if cached is not None and cached[0] == signature:
                return list(cached[1])
        stickers = self._load(folder)
        with self._lock:
            self._cache[folder.name] = (signature, stickers)
        return list(stickers)

    @staticmethod
    def _signature(folder: pathlib.Path) -> tuple:
        try:
            return tuple(sorted((entry.name, entry.stat().st_mtime_ns)
                                for entry in folder.iterdir() if entry.is_file()))
        except OSError:
            return ()

    def _load(self, folder: pathlib.Path) -> list[Sticker]:
        entity = folder.name
        manifest = folder / "stickers.json"
        rows: list[dict] = []
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                rows = [row for row in (data.get("stickers") or []) if isinstance(row, dict)]
            except (OSError, ValueError, AttributeError):
                rows = []
        if not rows:
            rows = [{"id": path.stem.lower(), "file": path.name}
                    for path in sorted(folder.iterdir())
                    if path.is_file() and path.suffix.lower() in MIME]
        out: list[Sticker] = []
        seen: set[str] = set()
        for row in rows:
            sticker_id = str(row.get("id") or "").strip().lower()
            file = str(row.get("file") or "").strip()
            if not _ID.match(sticker_id) or sticker_id in seen:
                continue
            if pathlib.PurePath(file).suffix.lower() not in MIME or "/" in file or "\\" in file:
                continue
            if not (folder / file).is_file():
                continue
            label = str(row.get("label") or "").strip() or sticker_id
            meaning = str(row.get("meaning") or "").strip() or label
            seen.add(sticker_id)
            out.append(Sticker(sticker_id, entity, file, label[:24], meaning[:80]))
        return out


__all__ = ["MIME", "Sticker", "StickerCatalog"]
