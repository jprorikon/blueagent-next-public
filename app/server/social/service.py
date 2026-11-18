"""公共区独立业务核：身份、可见域、限流、投稿、回复、点赞、删除与分页。"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from collections import deque

from app.server.social.feed import excerpt, page
from app.server.social.models import Actor, BoardError, Post
from app.server.social.store import BoardStore

MAX_BODY = 2000
MAX_MEDIA = 4
RATE_COUNT = 5
RATE_WINDOW_S = 60.0
_MEDIA_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SOURCE_REF = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,31}:[A-Za-z0-9][A-Za-z0-9:._/-]{0,220}")


class BoardService:
    def __init__(self, data_root, *, feedback=None, names: dict | None = None,
                 clock=time.time, enabled: bool | None = None, media_owner=None,
                 projection=None) -> None:
        self.data_root = data_root
        self.store = BoardStore(data_root)
        self.feedback = feedback
        self.names = {str(key): str(value) for key, value in (names or {}).items()}
        self._clock = clock
        self.media_owner = media_owner
        self.projection = projection
        if enabled is None:
            enabled = str(os.environ.get("BLUEAGENT_BOARD") or "1").strip().lower() \
                not in {"0", "off", "false", "no"}
        self.enabled = bool(enabled)
        #: 回填帖（带 occurred_at）在**真实时间**上的发帖时刻：它们的 `at` 在过去，
        #: 按 `at` 数不进限流窗口；单独记在这里（只需覆盖一个 60 秒窗口，重启清空无妨）。
        self._backdated: dict[str, deque] = {}
        self._backdated_lock = threading.Lock()

    def close(self) -> None:
        self.store.close()

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise BoardError(403, "board_disabled")

    @staticmethod
    def _body_media(body, media) -> tuple[str, list[str]]:
        text = str(body or "").strip()
        if len(text) > MAX_BODY:
            raise BoardError(413, "body_too_long", limit=MAX_BODY)
        if not isinstance(media, (list, tuple)):
            raise BoardError(400, "bad_media")
        found = []
        for value in media:
            value = str(value or "").strip()
            if not _MEDIA_ID.fullmatch(value):
                raise BoardError(400, "bad_media")
            if value not in found:
                found.append(value)
        if len(found) > MAX_MEDIA:
            raise BoardError(413, "too_many_media", limit=MAX_MEDIA)
        if not text and not found:
            raise BoardError(400, "empty_post")
        return text, found

    def _rate(self, actor: Actor, now: float, *, backdated_at: float | None = None) -> None:
        """每分钟限流，一律按**服务端真实时间**算（REVIEW 2026-11-08）。

        以前把回填帖的 `occurred_at` 当成 now 传进来 ⇒ 窗口落在过去，数到的是「那之后的所有帖」，
        旧活动的帖永远 429，发帖器按旧→新顺序重试就整条卡死。回填帖的 `at` 在过去，
        DB 按 `at` 数不到它们 ⇒ 另记一份真实发帖时刻，两边相加才是这一分钟真的发了几条。
        """
        since = now - RATE_WINDOW_S
        with self._backdated_lock:
            stamps = self._backdated.setdefault(actor.id, deque())
            while stamps and stamps[0] < since:
                stamps.popleft()
            recent = self.store.recent_manual_count(actor.id, since) + len(stamps)
            if recent >= RATE_COUNT:
                raise BoardError(429, "rate_limited", retry_after=int(RATE_WINDOW_S))
            # 落在窗口外的回填帖 DB 数不到 ⇒ 记真实时刻；窗口内的 DB 已经数了，不重复记
            if backdated_at is not None and backdated_at < since:
                stamps.append(now)

    #: 角色帖允许回填的窗口（秒）。发帖器在停机／限流后会补发过去的活动，
    #: 帖子时间用活动真正做完的时刻（REVIEW 2026-11-07 #7），但不能无限往回填。
    MAX_BACKDATE_S = 7 * 86400

    def _occurred_at(self, actor: Actor, source_ref: str, occurred_at: float) -> float | None:
        """角色自己的公开活动帖可以带 `occurred_at`；人类发帖一律按现在。"""
        at = float(occurred_at or 0.0)
        if at <= 0.0:
            return None                     # 省略＝现在（人类客户端的老行为）
        if actor.kind != "entity" or not source_ref:
            raise BoardError(400, "occurred_at_not_allowed")
        now = float(self._clock())
        if at > now + 1.0:
            raise BoardError(400, "occurred_at_in_future")
        if at < now - self.MAX_BACKDATE_S:
            raise BoardError(400, "occurred_at_too_old")
        return at

    def _new_manual(self, actor: Actor, body, media, *, parent_id: str = "",
                    media_owner=None, source_ref: str = "", at: float | None = None) -> Post:
        self._require_enabled()
        text, items = self._body_media(body, media)
        owner_of = self.media_owner if media_owner is None else media_owner
        if owner_of is not None and any(owner_of(item) != actor.id for item in items):
            raise BoardError(400, "unknown_media")
        real_now = float(self._clock())
        self._rate(actor, real_now, backdated_at=at)
        now = real_now if at is None else float(at)
        return Post(id="pst_" + secrets.token_hex(12), at=now, author_kind=actor.kind,
                    author_id=actor.id, display=actor.display or actor.id,
                    body=text, media=items, kind="reply" if parent_id else "post",
                    parent_id=parent_id, source_ref=source_ref)

    def _projection(self) -> list[Post]:
        return list(self.projection(self.store.data_root)) if self.projection is not None else []

    def _timeline(self) -> list[Post]:
        if self.projection is None:
            # 导入后的 projection 是平台快照；不再回读 Agent 的私有活动原文。
            return [post for post in self.store.feed_posts()
                    if self.enabled or post.origin == "projection"]
        projections = self._projection()
        states = self.store.projection_states()
        for post in projections:
            old = states.get(post.id)
            if old is not None and old["deleted_at"]:
                post.deleted_at = old["deleted_at"]
                post.deleted_by = old["deleted_by"]
        # 旧 Agent 回退仍以实时 public 原文为准，陈旧 DB 投影不能绕过撤权。
        return [*projections, *(self.store.manual_posts() if self.enabled else [])]

    def _visible(self, post: Post, actor: Actor) -> bool:
        if post.visibility != "public" or post.deleted_at:
            return False
        if post.origin == "projection" and actor.visible_entities is not None:
            return post.author_id in actor.visible_entities
        return True

    def can_read_media(self, actor: Actor, media_id: str, owner_principal: str = "") -> bool:
        """上传者始终可读；其他人只能读自己可见、未删除帖所引用的图片。"""
        if owner_principal and owner_principal == actor.id:
            return True
        return any(media_id in post.media and self._visible(post, actor)
                   for post in self._timeline())

    def references_media(self, media_id: str) -> bool:
        """有沒有任何一則帖（含已刪的）引用這張圖。給私聊刪圖前判斷用。"""
        return any(str(media_id) in post.media for post in self._timeline())

    def _find(self, post_id: str, actor: Actor) -> Post:
        stored = self.store.get(post_id)
        if stored is not None and (stored.origin == "manual" or self.projection is None):
            post = stored
        else:
            # Agent 回退中仍以实时 public 原文为准，陈旧 DB 投影不能兜底。
            post = next((item for item in self._projection() if item.id == post_id), None)
            if post is not None and stored is not None and stored.deleted_at:
                post.deleted_at = stored.deleted_at
                post.deleted_by = stored.deleted_by
        if post is None or not self._visible(post, actor):
            raise BoardError(404, "not_found")
        return post

    def _display(self, post: Post) -> str:
        return self.names.get(post.author_id) or post.display or post.author_id

    def _serialize(self, post: Post, actor: Actor, counters: dict, *, detail: bool) -> dict:
        short, truncated = excerpt(post.body)
        stat = counters.get(post.id, {"likes": 0, "replies": 0, "liked": False})
        return {
            "id": post.id, "at": post.at, "author_kind": post.author_kind,
            "author_id": post.author_id, "display": self._display(post),
            "origin": post.origin, "kind": post.kind,
            "body": post.body if detail else short, "truncated": truncated,
            "media": list(post.media), "visibility": post.visibility,
            "parent_id": post.parent_id, "likes": stat["likes"],
            "replies": stat["replies"], "liked": stat["liked"],
        }

    def feed(self, actor: Actor, *, before: str = "", limit: int = 20,
             since: float = 0.0, authors: set[str] | None = None) -> dict:
        visible = [post for post in self._timeline() if self._visible(post, actor)]
        result = page(visible, before=before, limit=limit, since=since, authors=authors)
        posts = result["posts"]
        counters = self.store.counters([post.id for post in posts], actor.id)
        return {"posts": [self._serialize(post, actor, counters, detail=False) for post in posts],
                "next_cursor": result["next_cursor"], "limit": max(1, min(60, int(limit or 20)))}

    def get(self, actor: Actor, post_id: str) -> dict:
        post = self._find(post_id, actor)
        replies = [reply for reply in self.store.replies(post.id) if self._visible(reply, actor)]
        counters = self.store.counters([post.id, *(reply.id for reply in replies)], actor.id)
        return {"post": self._serialize(post, actor, counters, detail=True),
                "replies": [self._serialize(reply, actor, counters, detail=True)
                            for reply in replies]}

    @staticmethod
    def _mutation(actor: Actor, mutation_id: str, kind: str, request: dict, at: float) -> dict | None:
        if not mutation_id:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(mutation_id)):
            raise BoardError(400, "bad_mutation_id")
        return {"actor_id": actor.id, "id": str(mutation_id), "kind": kind,
                "request": request, "at": float(at)}

    def post(self, actor: Actor, body, media=(), client_mutation_id: str = "", *,
             media_owner=None, source_ref: str = "", occurred_at: float = 0.0) -> dict:
        text, items = self._body_media(body, media)
        source_ref = str(source_ref or "").strip()
        if source_ref and not _SOURCE_REF.fullmatch(source_ref):
            raise BoardError(400, "bad_source_ref")
        at = self._occurred_at(actor, source_ref, occurred_at)
        request = {"text": text, "media": items}
        if source_ref:
            request["source_ref"] = source_ref
        if client_mutation_id:
            cached = self.store.mutation(actor.id, str(client_mutation_id), "post.create", request)
            if cached is not None:
                return cached
        if source_ref:
            existing = self.store.by_source_ref(source_ref)
            if existing is not None:
                same = existing.origin == "manual" and not existing.parent_id \
                    and existing.author_id == actor.id and existing.body == text \
                    and existing.media == items
                if not same:
                    raise BoardError(409, "source_ref_conflict")
                if existing.deleted_at:
                    raise BoardError(409, "source_ref_deleted")
                counters = self.store.counters([existing.id], actor.id)
                return {"post": self._serialize(existing, actor, counters, detail=True),
                        "idempotent_replay": True}
        post = self._new_manual(actor, text, items, media_owner=media_owner,
                                source_ref=source_ref, at=at)
        response = {"post": self._serialize(post, actor, {}, detail=True)}
        mutation = self._mutation(actor, str(client_mutation_id or ""), "post.create",
                                  request, post.at)
        if mutation is not None:
            mutation["response"] = response
        self.store.create_manual(post, mutation)
        return response

    def reply(self, actor: Actor, post_id: str, body, media=(), client_mutation_id: str = "", *,
              media_owner=None) -> dict:
        self._require_enabled()
        text, items = self._body_media(body, media)
        request = {"id": post_id, "text": text, "media": items}
        if client_mutation_id:
            cached = self.store.mutation(actor.id, str(client_mutation_id), "reply.create", request)
            if cached is not None:
                return cached
        parent = self._find(post_id, actor)
        if parent.parent_id:
            raise BoardError(409, "nested_reply_not_allowed")
        reply = self._new_manual(actor, text, items, parent_id=parent.id, media_owner=media_owner)
        response = {"post": self._serialize(reply, actor, {}, detail=True)}
        mutation = self._mutation(actor, str(client_mutation_id or ""), "reply.create",
                                  request, reply.at)
        if mutation is not None:
            mutation["response"] = response
        self.store.create_manual(reply, mutation,
                                 notice=self._notice(parent, actor, "reply", preview=reply.body,
                                                     reply_id=reply.id))
        return response

    @staticmethod
    def _notice(post: Post, actor: Actor, kind: str, *, preview: str = "",
                reply_id: str = "") -> dict | None:
        """帖主是人類、而且不是自己對自己 ⇒ 給他一條通知（B.App.8）；角色帖主照舊走 Agent 叫醒。"""
        if post.author_kind == "entity" or not post.author_id or post.author_id == actor.id:
            return None
        return {"recipient": post.author_id,
                "payload": {"kind": kind, "post_id": post.id, "reply_id": reply_id,
                            "actor_id": actor.id, "actor_kind": actor.kind,
                            "actor_display": actor.display or actor.id,
                            "preview": str(preview or "")[:80], "post_preview": post.body[:40]}}

    def like(self, actor: Actor, post_id: str) -> dict:
        self._require_enabled()
        post = self._find(post_id, actor)
        if post.origin == "projection" and self.store.get(post.id) is None:
            self.store.upsert_projection(post)
        added = self.store.like(post.id, actor.id, float(self._clock()), actor.kind,
                                notice=self._notice(post, actor, "like"))
        if added and post.author_kind == "entity" and self.feedback is not None:
            self.feedback(post.author_id, f"公共区の投稿にいいね：{post.id}")
        counters = self.store.counters([post.id], actor.id)
        return {"added": added, "post": self._serialize(post, actor, counters, detail=False)}

    def delete(self, actor: Actor, post_id: str) -> dict:
        self._require_enabled()
        post = self._find(post_id, actor)
        if not actor.is_owner and post.author_id != actor.id:
            raise BoardError(403, "denied")
        if post.origin == "projection" and self.store.get(post.id) is None:
            self.store.upsert_projection(post)
        self.store.soft_delete(post.id, actor.id, float(self._clock()), actor.kind)
        return {"ok": True, "id": post.id, "deleted": True}


__all__ = ["Actor", "BoardError", "BoardService", "MAX_BODY", "MAX_MEDIA",
           "RATE_COUNT", "RATE_WINDOW_S"]
