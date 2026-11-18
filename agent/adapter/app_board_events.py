"""公共区事件回流：有人回她的帖／赞她的帖 ⇒ 她知道（感知，不开 LLM 回合）。

owner 2026-11-07：「事件回流做」＋「自主动作改为从帖读，公共活动记录可以废除掉」。

所以这里**只做通知**，不做「自动回复」：

  · 一条世界事件进她的记忆／情绪（`world.publish("board", …)`），
  · 回不回、怎么回，是她自己在自主动作里用 `board.reply` 决定的事（带每日上限）。

两个必须的闸：

  · **只认她自己的帖**：`reply.created`／`post.liked` 都要现查那帖的作者（`lookup`），
    作者是她才算。**不靠「她自己发过哪些帖」的记忆**——上线前发的帖也要能被回。
    查不到（网络失败）就跳过，宁可不报也不误报。
  · **去重与上限**：同一条帖同一个人同一种动作 5 分钟内只报一次；每角色每天最多 40 条，
    免得公共区热闹起来把她的记忆灌满。

"""

from __future__ import annotations

import time

#: 关心的事件类型（其余事件与公共区无关）
KINDS = ("post.created", "reply.created", "post.liked")

#: 同一件事多久算「同一波」（秒）
DEDUPE_WINDOW = 300.0
#: 每角色每天最多回流多少条
NOTICE_PER_DAY = 40
def _clip(text, limit: int = 60) -> str:
    body = " ".join(str(text or "").split())
    return body if len(body) <= limit else body[:limit] + "…"


class BoardEventBridge:
    """把公共区事件翻成「她该知道的一句话」。真值仍在 App Server。"""

    def __init__(self, entity: str, sidecar, names=None, *, clock=time.time) -> None:
        self.entity = str(entity)
        self.sidecar = sidecar
        self.names = {str(key): str(value) for key, value in (names or {}).items()}
        self.clock = clock
        #: 腦端身分中樞（`brain/app/identity.py`）：App 帳號 id → principal；state 接線時給
        self.identity = None

    # ── 状态（跟游标档放一起，备份一起走） ───────────────────────────────────────
    def _load(self) -> dict:
        payload = self.sidecar.read("board")
        seen = payload.get("seen") if isinstance(payload.get("seen"), dict) else {}
        return {"day": str(payload.get("day") or ""), "sent": int(payload.get("sent") or 0),
                "seen": {str(key): float(value) for key, value in seen.items()}}

    def _save(self, state: dict) -> None:
        self.sidecar.write("board", {"day": state["day"], "sent": state["sent"],
                                     "seen": state["seen"]})

    def _roll(self, state: dict, now: float) -> None:
        today = time.strftime("%Y%m%d", time.localtime(now))
        if state["day"] != today:                 # 跨天：当日计数与去重表清零
            state["day"] = today
            state["sent"] = 0
            state["seen"] = {}
        horizon = now - DEDUPE_WINDOW
        state["seen"] = {key: at for key, at in state["seen"].items() if at >= horizon}

    # ── 判据 ───────────────────────────────────────────────────────────────────
    def consider(self, event: dict, lookup=None) -> dict | None:
        """返回一条感知（dict），或 None＝这件事与她无关（或已经报过）。"""
        event_type = str(event.get("type") or "")
        if event_type not in KINDS:
            return None
        payload = event.get("payload") or {}
        actor = event.get("actor") or {}
        actor_id = str(actor.get("id") or "")
        if not actor_id:
            return None
        now = self.clock()
        state = self._load()
        self._roll(state, now)

        if event_type == "post.created":
            return None                          # 发帖本身不回流（她自己发的也不用）



        if actor_id == self.entity:
            return None                          # 她自己的动作不回流（自己赞／自己回）

        if event_type == "reply.created":
            post = payload.get("post") or {}
            parent_id = str(post.get("parent_id") or "")
            parent = self._lookup(lookup, parent_id) if parent_id else {}
            if str(parent.get("author_id") or "") != self.entity:
                return None                      # 不是她的帖（或查不到）⇒ 不回流
            key = f"reply:{post.get('id') or parent_id}:{actor_id}"
            kind = "board.reply"
            who = self._who(actor_id)
            text = (f"{who}が、あなたの投稿「{_clip(parent.get('body'))}」に返事をくれた："
                    f"{_clip(post.get('body'), 120)}")
            post_id = parent_id
        else:                                    # post.liked
            post_id = str(payload.get("post_id") or "")
            parent = self._lookup(lookup, post_id) if post_id else {}
            if str(parent.get("author_id") or "") != self.entity:
                return None                      # 不是她的帖（或查不到）⇒ 不回流
            key = f"like:{post_id}:{actor_id}"
            kind = "board.like"
            who = self._who(actor_id)
            text = f"{who}が、あなたの投稿「{_clip(parent.get('body'))}」にいいねした"

        if key in state["seen"]:
            return None                          # 同一波只报一次
        if state["sent"] >= NOTICE_PER_DAY:
            return None
        state["seen"][key] = now
        state["sent"] += 1
        self._save(state)
        return {"kind": kind, "text": text, "post_id": post_id, "actor_id": actor_id}

    # ── 小工具 ─────────────────────────────────────────────────────────────────
    def _who(self, actor_id: str) -> str:
        who = self.identity.principal_of(actor_id) if self.identity is not None else actor_id
        if who == "owner":                    # 以前寫死 `startswith("acct_owner")`，客人就認不出來
            return "主人"
        return self.names.get(who) or self.names.get(actor_id) or who

    @staticmethod
    def _lookup(lookup, post_id: str) -> dict:
        if lookup is None:
            return {}
        try:
            payload = lookup(post_id) or {}
        except Exception:                        # noqa: BLE001 - 查不到就只写 id
            return {}
        post = payload.get("post") if isinstance(payload, dict) else None
        return post if isinstance(post, dict) else {}


__all__ = ["BoardEventBridge", "DEDUPE_WINDOW", "KINDS", "NOTICE_PER_DAY"]
