"""Agent 与客户端资产的窄边界：远程读取贴纸语义，旧端点按需取单张图。"""

from __future__ import annotations

import os
import pathlib
import re
import threading
import time
from dataclasses import dataclass

from agent.adapter.app_server import AppServerClient, _tokens

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


@dataclass(frozen=True, slots=True)
class RemoteSticker:
    id: str
    entity: str
    label: str
    meaning: str
    file: str = ""

    def to_dict(self, *, url_prefix: str = "/api/stickers") -> dict:
        # 旧回合帧仍引用 Agent 兼容端点；新 Unity 目录直接使用 App Server /v1 URL。
        prefix = "/" + str(url_prefix or "/api/stickers").strip("/")
        return {"id": self.id, "entity": self.entity, "label": self.label,
                "url": f"{prefix}/{self.entity}/{self.id}"}


class RemoteStickerCatalog:
    """按角色缓存语义目录；网络失败时使用旧缓存，没有缓存则让贴纸工具消失。"""

    def __init__(self, base_url: str, tokens: dict[str, str], cache_dir, *,
                 timeout: float = 5.0, ttl: float = 300.0,
                 backoff: float = 45.0,
                 client_factory=AppServerClient, clock=time.monotonic) -> None:
        self.cache_root = pathlib.Path(cache_dir)
        self.ttl = max(1.0, float(ttl))
        #: 拉取失败后旧缓存／负缓存的有效期（秒）：REVIEW 2026-11-07 #5。
        #: 不推这段时间的话，App Server 一不通，每次 ``list()`` 都要重新等满 timeout。
        self.backoff = max(1.0, float(backoff))
        self.clock = clock
        self._clients = {str(entity): client_factory(base_url, token, timeout=timeout)
                         for entity, token in tokens.items()}
        self._cache: dict[str, tuple[float, list[RemoteSticker]]] = {}
        self._hashes: dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()
        self._entity_locks: dict[str, threading.Lock] = {}

    def _entity_lock(self, entity: str) -> threading.Lock:
        with self._lock:
            return self._entity_locks.setdefault(entity, threading.Lock())

    def list(self, entity: str) -> list[RemoteSticker]:
        entity = str(entity or "").strip().lower()
        if not _ID.fullmatch(entity) or entity not in self._clients:
            return []
        with self._entity_lock(entity):
            with self._lock:
                cached = self._cache.get(entity)
                if cached is not None and cached[0] > self.clock():
                    return list(cached[1])
            try:
                payload = self._clients[entity].sticker_list(entity)
                rows = []
                hashes = {}
                seen = set()
                for row in payload.get("stickers") or []:
                    if not isinstance(row, dict):
                        continue
                    sticker_id = str(row.get("id") or "").strip().lower()
                    if not _ID.fullmatch(sticker_id) or sticker_id in seen:
                        continue
                    seen.add(sticker_id)
                    label = str(row.get("label") or sticker_id)[:24]
                    meaning = str(row.get("meaning") or label)[:80]
                    rows.append(RemoteSticker(sticker_id, entity, label, meaning))
                    hashes[(entity, sticker_id)] = str(row.get("hash") or "")
            except Exception:                            # 网络／协议故障不拖垮聊天
                with self._lock:
                    cached = self._cache.get(entity)
                    if cached is not None:
                        # 旧列表继续用，但把那一段过期时间往后推 ⇒ 别每次都重新等超时
                        self._cache[entity] = (self.clock() + self.backoff, cached[1])
                        return list(cached[1])
                    # 从来没有成功过：记一段短期负缓存（空列表），同样防止每轮重试
                    self._cache[entity] = (self.clock() + self.backoff, [])
                return []
            with self._lock:
                self._cache[entity] = (self.clock() + self.ttl, list(rows))
                self._hashes.update(hashes)
            return rows

    def get(self, entity: str, sticker_id: str) -> RemoteSticker | None:
        wanted = str(sticker_id or "").strip().lower()
        return next((item for item in self.list(entity) if item.id == wanted), None)

    def hash_of(self, sticker: RemoteSticker) -> str:
        if sticker is None:
            return ""
        with self._lock:
            return self._hashes.get((sticker.entity, sticker.id), "")

    def path_of(self, sticker: RemoteSticker) -> pathlib.Path | None:
        """仅供旧 ``/api/stickers`` 文件端点使用，按需缓存单张且保留正确后缀。"""
        if sticker is None or sticker.entity not in self._clients:
            return None
        with self._entity_lock(sticker.entity):
            folder = self.cache_root / sticker.entity
            digest = self.hash_of(sticker)
            for cached_suffix in (".png", ".jpg"):
                if digest:
                    cached = folder / f"{sticker.id}-{digest}{cached_suffix}"
                    if cached.is_file():
                        return cached
                # REVIEW 2026-11-07 #9：没有 hash 的素材也会落 ``-current`` 档，
                # 以前只写不查 ⇒ 每次旧端点请求都多一次网络往返。
                current = folder / f"{sticker.id}-current{cached_suffix}"
                if current.is_file():
                    return current
            try:
                found = self._clients[sticker.entity].read_sticker(sticker.entity, sticker.id)
            except Exception:
                return None
            suffix = {"image/png": ".png", "image/jpeg": ".jpg"}.get(found.get("mime"), "")
            data = found.get("bytes") or b""
            if not suffix or not data:
                return None
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{sticker.id}-{digest or 'current'}{suffix}"
            temporary = folder / f".{sticker.id}-{threading.get_ident()}.tmp{suffix}"
            try:
                temporary.write_bytes(data)
                os.replace(temporary, target)
            except OSError:
                temporary.unlink(missing_ok=True)
                return None
            return target


def build_sticker_catalog(*, data_dir, env=None):
    """显式本地目录优先；配置 App Server 后远程取语义，否则使用开发素材包。"""
    env = os.environ if env is None else env
    from app.client.assets.stickers import StickerCatalog

    override = str(env.get("BLUEAGENT_STICKER_DIR") or "").strip()
    if override:
        return StickerCatalog(override)
    base_url = str(env.get("BLUEAGENT_APP_SERVER_URL") or "").strip()
    tokens = _tokens(str(env.get("BLUEAGENT_APP_SERVER_TOKENS") or ""))
    if base_url and tokens:
        return RemoteStickerCatalog(
            base_url, tokens, pathlib.Path(data_dir) / "app-adapter" / "sticker-cache")
    root = pathlib.Path(__file__).resolve().parents[2] / "app" / "client" / "assets" / "stickers"
    return StickerCatalog(root)


__all__ = ["RemoteSticker", "RemoteStickerCatalog", "build_sticker_catalog"]
