"""App Server 贴纸目录与认证内容端点；素材属于客户端，不暴露文件路径。"""

from __future__ import annotations

import hashlib
import json
import pathlib
import threading

from app.client.assets.stickers import MIME, StickerCatalog
from app.server.media import THUMB_WIDTHS, thumbnail
from app.server.social.models import Actor, BoardError


class StickerService:
    def __init__(self, client_assets, data_root) -> None:
        self.catalog = StickerCatalog(pathlib.Path(client_assets) / "stickers")
        self.thumb_root = pathlib.Path(data_root) / "thumbs"
        self._thumb_lock = threading.Lock()

    @staticmethod
    def _authorize(actor: Actor, entity: str) -> str:
        entity = str(entity or "").strip().lower()
        visible = actor.visible_entities is None or entity in actor.visible_entities \
            or actor.id == entity
        # Agent 通过自己的账号只取自己的语义目录；owner／guest 仍受 visible_entities 限制。
        if actor.kind == "entity" and actor.id != entity:
            visible = False
        if not visible:
            raise BoardError(404, "not_found")
        return entity

    def list(self, actor: Actor, entity: str) -> dict:
        entity = self._authorize(actor, entity)
        rows = [dict(item.to_dict(url_prefix="/v1/stickers"),
                     meaning=item.meaning, hash=self.catalog.hash_of(item))
                for item in self.catalog.list(entity)]
        encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        version = hashlib.sha256(encoded).hexdigest()[:16]
        return {"entity": entity, "stickers": rows, "version": version}

    def read(self, actor: Actor, entity: str, sticker_id: str, *, width: int = 0):
        entity = self._authorize(actor, entity)
        found = self.catalog.get(entity, sticker_id)
        path = self.catalog.path_of(found) if found is not None else None
        if path is None:
            raise BoardError(404, "not_found")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise BoardError(404, "not_found") from exc
        tag = self.catalog.hash_of(found)
        if not tag:
            raise BoardError(404, "not_found")
        mime = MIME[path.suffix.lower()]
        if width in THUMB_WIDTHS:
            tag += f"-w{width}"
            small = thumbnail(path, self.thumb_root, width, self._thumb_lock)
            if small is not None:
                try:
                    data = small.read_bytes()
                except OSError as exc:
                    raise BoardError(404, "not_found") from exc
                mime = "image/png" if small.suffix == ".png" else "image/jpeg"
        return data, mime, tag


__all__ = ["StickerService"]
