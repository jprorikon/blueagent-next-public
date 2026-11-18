"""App adapter：消费平台会话事件，感知与回复分离，最终回复只经 MCP 提交。"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from agent.adapter import app_dm_sleep, app_group_delay, app_identity  # 睡著時私聊先放著；群聊延遲隊列；帳號映射
from agent.adapter.app_adapter_store import AdapterSidecar, fatal_failure as _fatal_failure
from agent.adapter.app_group import AttentionDecision, GroupAttention, GroupLedger
from agent.adapter.app_group_knock import knock_tick
from agent.adapter.app_read_tools import _tokens, room_note  # _tokens：tools／app_assets 也從這裡拿
from agent.adapter.app_board_events import KINDS as BOARD_EVENTS, BoardEventBridge


class AppServerClient:
    """窄 HTTP/MCP 客户端；token 只存在实例内，不写日志或游标文件。"""

    def __init__(self, base_url: str, token: str, *, timeout: float = 15.0) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.token = str(token)
        self.timeout = float(timeout)

    def _request(self, method: str, path: str, body=None) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read())
                code = str(payload.get("error") or exc.code)
            except Exception:
                code = str(exc.code)
            raise RuntimeError(f"app_server_http_{code}") from None
        return json.loads(raw) if raw else {}

    def me(self) -> dict:
        return self._request("GET", "/v1/me")

    def media_metadata(self, media_id: str) -> dict:
        reply = self._request("POST", "/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "media.get", "arguments": {"id": str(media_id)}}})
        result = reply.get("result") or {}
        if result.get("isError"):
            content = result.get("content") or [{}]
            raise RuntimeError("app_server_mcp_" + str(content[0].get("text") or "error"))
        return json.loads((result.get("content") or [{}])[0].get("text") or "{}")

    def read_media(self, media_id: str) -> dict:
        """以本角色 token 下载已授权图片；绝不接受服务器文件路径或任意外部 URL。"""
        request = urllib.request.Request(
            self.base_url + "/v1/media/" + urllib.parse.quote(str(media_id), safe=""),
            method="GET", headers={"Authorization": "Bearer " + self.token})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read(6 * 1024 * 1024 + 1)
                mime = response.headers.get_content_type()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"app_server_http_{exc.code}") from None
        if len(data) > 6 * 1024 * 1024 or mime not in {"image/png", "image/jpeg", "image/webp"}:
            raise RuntimeError("app_server_media_invalid")
        return {"id": str(media_id), "bytes": data, "mime": mime}

    def sticker_list(self, entity: str) -> dict:
        return self.board_call("sticker.list", {"entity": str(entity)})

    def read_sticker(self, entity: str, sticker_id: str) -> dict:
        """按需取一张客户端素材；服务器路径和整包本地副本都不进入 Agent。"""
        path = "/v1/stickers/{}/{}".format(
            urllib.parse.quote(str(entity), safe=""),
            urllib.parse.quote(str(sticker_id), safe=""))
        request = urllib.request.Request(
            self.base_url + path, method="GET",
            headers={"Authorization": "Bearer " + self.token})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read(6 * 1024 * 1024 + 1)
                mime = response.headers.get_content_type()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"app_server_http_{exc.code}") from None
        if len(data) > 6 * 1024 * 1024 or mime not in {"image/png", "image/jpeg"}:
            raise RuntimeError("app_server_sticker_invalid")
        return {"id": str(sticker_id), "entity": str(entity), "bytes": data, "mime": mime}

    def board_call(self, name: str, arguments: dict) -> dict:
        """远程公共区／媒体 MCP；业务错误保留机器可读 code，传输失败由兼容壳映射。"""
        from agent.adapter.app_protocol import BoardError

        reply = self._request("POST", "/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": str(name), "arguments": dict(arguments)}})
        result = reply.get("result") or {}
        content = result.get("content") or [{}]
        text = str(content[0].get("text") or "")
        if result.get("isError"):
            statuses = {
                "unauthorized": 401, "forbidden": 403, "denied": 403,
                "invalid_board_actor": 403, "not_found": 404,
                "mutation_conflict": 409, "source_ref_conflict": 409,
                "source_ref_deleted": 409, "nested_reply_not_allowed": 409,
                "body_too_long": 413, "too_many_media": 413,
                "rate_limited": 429, "media_unavailable": 503,
            }
            raise BoardError(statuses.get(text, 400), text or "app_server_error")
        return json.loads(text or "{}")

    def events(self, after: str, limit: int = 100) -> dict:
        return self._request("GET", "/v1/events?" + urllib.parse.urlencode(
            {"after": str(after), "limit": int(limit)}))

    def open_direct(self, account_id: str) -> dict:
        return self._request("POST", "/v1/conversations/direct", {"account_id": str(account_id)})

    def mark_read(self, conversation_id: str, message_id: str) -> dict:
        return self._request("POST", "/v1/conversations/" +
                             urllib.parse.quote(str(conversation_id), safe="") + "/read",
                             {"message_id": str(message_id)})

    def send_message(self, conversation_id: str, parts: list[str], *, mutation_id: str,
                     causation_id: str) -> dict:
        reply = self._request("POST", "/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "chat.message.send", "arguments": {
                "conversation_id": conversation_id, "parts": parts,
                "client_mutation_id": mutation_id, "causation_id": causation_id}}})
        result = reply.get("result") or {}
        if result.get("isError"):
            content = result.get("content") or [{}]
            raise RuntimeError("app_server_mcp_" + str(content[0].get("text") or "error"))
        return json.loads((result.get("content") or [{}])[0].get("text") or "{}")


class AppChatAdapter:
    """一个角色一条订阅循环。

    游标语义（2026-11-15 WP8）：
      · 即时事件（私聊、公共区／位置事件）在回合与 MCP 提交都结束后才持久推进；
      · 群聊事件在预占 `GroupLedger` 名额并原子写入 `<实体>-group-delay.json` 到期队列后立即推进游标
        （零延迟的也入队）；到期后由**回合工作线程** `ba-app-<实体>-turns` 重验并执行一次
        （进程重启可从磁盘恢复）。定时敲门／开场也在那条线程里——拉取线程不跑群聊的模型回合（2026-11-16）。
    """

    def __init__(self, entity: str, client, send, cursor_path, reply_text, reply_bubbles,
                 *, log=None, group_attention=None, group_ledger=None,
                 on_location=None, on_board=None, aliases=None,
                 max_attempts: int = 5, names=None, circadian=None,
                 min_retry_s: float = 600.0, clock=time.time, opener=None,
                 budget=None, activity_log=None) -> None:
        self.entity = str(entity)
        self.client = client
        self.send = send
        # 由 Agent 组合根注入结果解析端口；adapter 不反向 import app 层。
        self.reply_text = reply_text
        self.reply_bubbles = reply_bubbles
        self.cursor_path = pathlib.Path(cursor_path)
        self.log = log or (lambda _message: None)
        #: 群聊叫名（REVIEW 2026-11-07 #4）：entity → 她自己的叫名（含 id／显示名）。
        #: `@` 拿起率 0.95（随生物钟打折）、句首叫名 0.70，且各保留一个发言名额（M5）。
        self.aliases = {str(key).strip().lower(): tuple(str(word) for word in (words or ())
                                                        if str(word).strip())
                        for key, words in (aliases or {}).items()}
        #: 同一个事件连续失败几次就记 dead-letter 跳过（REVIEW 2026-11-07 #3）。
        self.max_attempts = max(1, int(max_attempts))
        #: 非確定性錯誤至少重試這麼久才放棄（REVIEW 2026-11-08：以前≈10 秒，部署一下就被 dead-letter）
        self.min_retry_s = max(0.0, float(min_retry_s))
        self._clock = clock
        self._first_failure: dict[str, float] = {}
        self._fail_streak = 0
        self.group_attention = group_attention if group_attention is not None \
            else GroupAttention(circadian=circadian)
        #: 三人共用的场账本（M5a）：拿起决定、发言名额、场／互接链、泡泡上限
        self.group_ledger = group_ledger if group_ledger is not None \
            else GroupLedger(self.cursor_path.with_name("group-ledger.json"), clock=clock)
        self.on_location = on_location
        #: 公共区事件回流（owner 2026-11-07）：有人回她的帖／赞她的帖 ⇒ 一条世界事件。
        self.on_board = on_board
        self._stop = threading.Event()
        #: 推送叫醒（`docs/PLAN-app-kick-20261108.md`）：App Server 一有事件就 `wake()`，
        #: 讓下面的等待立刻結束、馬上拉一次；沒有 kick 時靠 `_idle_s` 兜底對賬。
        self._wake = threading.Event()
        self._idle_s = max(0.2, float(os.environ.get("BLUEAGENT_APP_POLL_IDLE_S") or 20.0))
        self._thread = None
        #: 群聊回合工作线程（2026-11-16）：到期回合＋敲门在这里跑，拉取线程只入队。
        #: 没起（测试直接调 `poll_once`）⇒ `poll_once` 当场结算，语义不变。
        self._turn_thread = None
        self._turn_wake = threading.Event()
        #: 延迟队列档的读改写锁（拉取线程入队 ／ 工作线程认领、移除）
        self._delay_lock = threading.RLock()
        self._cursor = self._load_cursor()
        self._primed = bool(self._cursor)
        self.sidecar = AdapterSidecar(self.cursor_path)
        self.board_events = BoardEventBridge(self.entity, self.sidecar, names=names)
        #: 定时敲门（GROUP-SELF-CHAT §2 ①）：没人说话时自己开场的端口（`compose`／`record`），没有就只补看
        self.opener = opener
        self.names = dict(names or {})
        self._next_knock = 0.0
        self._seen_before = 0.0
        #: 行動預算（owner 2026-11-08）：**角色↔角色的私聊算她自己的社交活動**，要付錢。
        self.budget = budget
        #: 她自己的行動日誌：私聊也留一行，日報／心跳看得到她「跟誰聊過」。
        self.activity_log = activity_log

    def _load_cursor(self) -> str:
        try:
            payload = json.loads(self.cursor_path.read_text(encoding="utf-8"))
            return str(payload.get("cursor") or "")
        except (OSError, ValueError, TypeError):
            return ""

    def _save_cursor(self, cursor: str) -> None:
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cursor_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"cursor": str(cursor)}), encoding="utf-8")
        os.replace(temporary, self.cursor_path)
        self._cursor = str(cursor)

    def prime(self) -> None:
        """首次上线不回复历史 backlog，只把游标推到当前末尾。"""
        cursor = "0"
        while True:
            page = self.client.events(cursor, limit=200)
            cursor = str(page.get("next_cursor") or cursor)
            if not page.get("has_more"):
                break
        self._save_cursor(cursor)
        self._primed = True

    def poll_once(self) -> int:
        if not self._primed:
            self.prime()
            return 0
        page = self.client.events(self._cursor, limit=100)
        processed = 0
        enqueued: set[str] = set()
        for event in page.get("events") or []:
            key = str(event.get("event_id") or event.get("cursor") or "")
            self._run_event(event, key)
            self._save_cursor(str(event.get("cursor") or event.get("event_id") or self._cursor))
            processed += 1
            if event.get("_enqueued_key"):
                enqueued.add(str(event["_enqueued_key"]))
        if self._turn_thread is None:              # 没有工作线程（测试）⇒ 当场结算
            processed += app_group_delay.tick(self, float(self._clock()), skip_count=enqueued)
        return processed

    def _run_event(self, event: dict, key: str) -> None:
        try:
            self._handle(event)
        except Exception as exc:
            # 一件永远处理不了的事件以前会把整条线卡死（REVIEW 2026-11-07 #3）：
            # 确定性错误（404／403／内容不合法）立刻 dead-letter，其他错误最多重试 max_attempts 次。
            attempts = self.sidecar.note_failure(key)
            first = self._first_failure.setdefault(key, float(self._clock()))
            fatal = _fatal_failure(exc)
            # 只差送出的回覆（unsent）遇非確定性錯誤一律續試：只補發、不重跑（REVIEW 2026-11-08）
            pending_reply = self._request_id(event) in self.sidecar.unsent
            exhausted = attempts >= self.max_attempts and self._clock() - first >= self.min_retry_s
            if fatal or (exhausted and not pending_reply):
                self.sidecar.dead_letter(key, exc, event, entity=self.entity, log=self.log)
                self.sidecar.drop_unsent(self._request_id(event))
                self._first_failure.pop(key, None)
                return
            raise
        self.sidecar.clear_failure(key)
        self._first_failure.pop(key, None)

    def _handle(self, event: dict) -> None:
        event_type = str(event.get("type") or "")
        if event_type in BOARD_EVENTS:
            self._on_board_event(event)
            return
        if event_type == "chat.read.updated":     # 對方讀到哪（主動消息的已讀，`app_proactive.py`）
            if getattr(self, "on_read", None) is not None:
                self.on_read(self.entity, dict(event.get("payload") or {}))
            return
        if event_type in {"location.updated", "location.revoked"}:
            if self.on_location is not None:
                self.on_location(self.entity, event_type, dict(event.get("payload") or {}))
            return
        if event_type != "chat.message.created":
            return
        payload = event.get("payload") or {}
        message = payload.get("message") or {}
        author = message.get("author") or {}
        principal = app_identity.principal_of(self, str(author.get("id") or ""))
        conversation_id = str(payload.get("conversation_id") or
                              message.get("conversation_id") or "")
        if not principal or not conversation_id or principal == self.entity:
            return                           # 自己产生的事件绝不再次触发自己
        if str((event.get("actor") or {}).get("id") or "") == self.entity:
            return
        original_parts = [dict(part) for part in (message.get("parts") or [])
                          if isinstance(part, dict)]
        text = "\n".join(str(part.get("text") or "").strip() for part in original_parts
                         if str(part.get("text") or "").strip()).strip()
        photo_ids = [str(part.get("media_id") or "") for part in original_parts
                     if str(part.get("kind") or "") == "photo" and part.get("media_id")]
        if not text and not photo_ids:
            return                           # 空事件与纯表情仍不走图片回合

        is_group = str(payload.get("conversation_kind") or "direct") == "group"
        responder = True
        message_id = str(message.get("id") or "")

        # ② 上次回合跑完、但提交没送到（App Server 502 之类）：先把那份回复补发出去，
        # 不要重跑一轮 LLM——重跑会被幂等窗判重复、回复永远发不出去（REVIEW #2）。
        request_id = self._request_id(event)
        pending = self.sidecar.unsent.get(request_id) if request_id else None
        if pending and str(pending.get("conversation_id") or "") == conversation_id \
                and pending.get("parts"):
            self.log(f"app adapter[{self.entity}]: 补发上次没送出的回复 event={request_id}")
            self._deliver(conversation_id, [str(part) for part in pending["parts"]],
                          request_id, event)
            return

        if not is_group and app_dm_sleep.hold(self, event, conversation_id, message, author, text):
            return                           # 她睡著：先放著，醒來（`app_dm_sleep.tick`）一次讀完
        # 角色↔角色：direct 会话里对方也是 agent ⇒ 这是**她自己的社交**，不是主人派的事。
        # 记预算（付不出就不接话，互刷会自然停下来）＋留一行行动日志（owner 2026-11-08）。
        if not is_group and str(author.get("kind") or "") == "entity":
            if not self._pay_for_peer_chat(principal, text, message_id):
                return

        if is_group:
            responder = self._group_turn(conversation_id, message_id, message, author, text,
                                         knocked=bool(event.get("_knock")),
                                         participants=app_identity.people(self, payload.get("participants")),
                                         event=event)
            if responder is None:
                return                       # 没拿起手机或已进延迟队列：不下载、不注入、不标记已读

        # 群聊先感知再下载。网络类失败向上抛出（整件重试）；图片本身没了（404）就退化成
        # 纯文字回合，不要让一张被删掉的图卡死整条线（REVIEW #3）。
        media_list = []
        for media_id in photo_ids:
            try:
                media_list.append(self.client.read_media(media_id))
            except Exception as exc:
                if not _fatal_failure(exc):
                    raise
                self.log(f"app adapter[{self.entity}]: 图片 {media_id} 取不到（{exc}）"
                         " ⇒ 这一轮只读文字")
        if not text.strip() and not media_list:
            self.log(f"app adapter[{self.entity}]: event={request_id} 只有读不到的图片 ⇒ 跳过")
            return
        if is_group and message_id:
            self.client.mark_read(conversation_id, message_id)
        kwargs = {"entity": self.entity, "principal": principal,
                  "source_override": "app:" + conversation_id, "request_id": request_id,
                  "mode": "work" if str(author.get("kind") or "") == "owner" else "chat",  # 主人＝work（同 HTTP 閘／Discord）
                  **app_dm_sleep.extra(event)}
        if media_list:
            kwargs.update(kind="image", media_list=media_list, asked_parts=original_parts)
        if is_group:
            participants = set(app_identity.people(self, payload.get("participants"))) \
                | {principal, self.entity}
            # ① App 群聊用**自己的** scope／scene，不要和 Discord 大厅共用一条 thread
            # （REVIEW #1：以前 owner 在 App「3人の部屋」说的话会写进 Discord 大厅那条线）。
            # 未获选／其他 Agent 的发言仍可进入旁观上下文，但绝不能产生回复。
            kwargs.update(group=True, participants=sorted(participants),
                          addressed_to="" if responder else "ignore",
                          scope_override="group:" + conversation_id,
                          scene_override="app-group",
                          room_note=self._room_note(conversation_id, message_id))
        result = self.send(text or "（画像）", **kwargs)
        if is_group and (not responder or bool(result.get("observed"))):
            return                           # 已读不回合法；获选者沉默也不让别人补位
        reply = self.reply_text(result)
        bubbles = self.reply_bubbles(result)
        if not reply and not bubbles:
            if result.get("duplicate"):
                self.log(f"app adapter[{self.entity}]: event={request_id} 判重复且没有待补发内容")
            return
        parts = list(bubbles or [reply])
        if is_group:
            parts = parts[:3]                # C-12：一轮自主组织 1–3 个有序泡泡
            if not self.group_ledger.commit(self.entity, len(parts)):
                self.log(f"app adapter[{self.entity}]: 群聊泡泡到上限 ⇒ 这一轮不发")
                return                       # 写库前再判一次（模型调用期间别人可能用掉了额度）
        self._deliver(conversation_id, parts, request_id, event)

    def speaker_name(self, account: str, kind: str = "") -> str:
        return "オーナー" if kind == "owner" else self.names.get(account, account)

    def _pay_for_peer_chat(self, peer: str, text: str, message_id: str) -> bool:
        """角色私聊的「自主活動」記帳：扣預算，付不出就不回。

        為什麼不是主人發起的那條：那條不該被預算擋（`budget.py` 的三檔規則）。
        但私聊是**她自己想聊**——跟自己去讀書一樣要花額度，不然兩個 agent 會互相刷到天亮
        （実測 11-06：942 条「……う」）。`cost_for("social")` 可用 `BLUEAGENT_BUDGET_PRICES` 覆寫。
        """
        name = str(self.names.get(peer) or peer)
        cost = 1.0
        paid = True
        if self.budget is not None:
            try:
                cost = float(self.budget.cost_for("social"))
                paid = bool(self.budget.try_spend(self.entity, cost,
                                                  reason=f"peer-chat:{peer}",
                                                  src=f"peer-chat:{message_id}"))
            except Exception as exc:                       # noqa: BLE001 - 記帳壞了不能擋對話
                self.log(f"app adapter[{self.entity}]: 私聊记帳失敗（{type(exc).__name__}）⇒ 照常回")
        if not paid:
            self.log(f"app adapter[{self.entity}]: 私聊預算不足（{cost:.1f}）⇒ 不回 {name}")
            return False
        if self.activity_log is not None:
            try:
                self.activity_log.append(
                    self.entity, kind="social", visibility="private",
                    text=f"{name}と私聊：{(text or '（画像）')[:80]}")
            except Exception:                              # noqa: BLE001
                pass
        return True

    def _group_turn(self, conversation_id: str, message_id: str, message: dict,
                    author: dict, text: str, *, knocked: bool = False, participants=(),
                    event: dict | None = None):
        return app_group_delay.group_turn(
            self, event if event is not None else {}, conversation_id, message_id,
            message, author, text, knocked=knocked, participants=participants)

    def _room_note(self, conversation_id: str, message_id: str) -> str:
        """hybrid 前後文：她上次看過之後的那幾則直接附上，更早的讓她用 `app_chat_read` 自己讀。

        取不到（網路）就只附工具提示——不讓一次讀取失敗卡住這一輪。
        """
        try:
            rows = self.client.board_call("chat.messages", {"conversation_id": conversation_id,
                                                           "limit": 12}).get("messages") or []
        except Exception as exc:
            self.log(f"app adapter[{self.entity}]: 取房間前文失敗（{type(exc).__name__}）")
            rows = []
        return room_note(rows, conversation_id=conversation_id, since=self._seen_before,
                         skip_id=message_id, me=self.entity, names=self.names)

    def _on_board_event(self, event: dict) -> None:
        """公共区事件：只做**感知回流**，不开回合、不自动回帖（owner 2026-11-07）。

        她的回复能力在自主动作的 `board.reply`／`board.like`（带每日上限）——见
        `brain/runtime/tools/board.py`。这里只让她「知道」。
        """
        notice = self.board_events.consider(event, lookup=self._lookup_post)
        if notice is None or self.on_board is None:
            return
        self.log(f"app adapter[{self.entity}]: 公共区回流 {notice['kind']} post={notice['post_id']}")
        self.on_board(self.entity, notice["kind"], notice, event)

    def _lookup_post(self, post_id: str) -> dict:
        """点赞事件只带 post_id，正文要现查（查不到就只写 id，不影响回流）。"""
        return self.client.board_call("get", {"id": str(post_id)})

    @staticmethod
    def _request_id(event: dict) -> str:
        """回合的冪等鍵＝訊息 id（沒有就用事件 id）；unsent 補發也用它找回那一份。"""
        return str(((event.get("payload") or {}).get("message") or {}).get("id")
                   or event.get("event_id") or "")

    def _deliver(self, conversation_id: str, parts: list, request_id: str, event: dict) -> None:
        """先落盘再提交：提交失败时下一次重试直接补发这一份，不重跑回合（REVIEW #2）。"""
        entry = {"conversation_id": str(conversation_id), "parts": [str(part) for part in parts],
                 "causation_id": str(event.get("event_id") or ""), "at": time.time()}
        if request_id:
            self.sidecar.put_unsent(request_id, entry)
        try:
            self.client.send_message(conversation_id, entry["parts"],
                                     mutation_id="agent:" + str(request_id),
                                     causation_id=entry["causation_id"])
        except Exception:
            raise                            # 留着 unsent 档，下一轮补发
        if request_id:
            self.sidecar.drop_unsent(request_id)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._turn_thread = threading.Thread(target=self._turn_loop, daemon=True,
                                             name=f"ba-app-{self.entity}-turns")
        self._turn_thread.start()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"ba-app-{self.entity}")
        self._thread.start()

    def wake_turns(self) -> None:
        """有新的群聊条目（含现在就到期的）：叫醒回合工作线程。"""
        self._turn_wake.set()

    def wake(self) -> None:
        """有人叫她（`POST /api/app/kick`）：結束這一輪等待，下一輪立刻拉事件。

        **只置位、不排隊**：連按十次 kick 也只多拉一次（真正推進游標的是拉取）。
        """
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # 网络/回合失败保留游标，退避后重试
                self.log(f"app adapter[{self.entity}] {type(exc).__name__}: {exc}")
                self._fail_streak += 1         # 連續失敗拉長間隔 2→4→…→60 秒
                self._wake.clear()
                self._wake.wait(min(60.0, 2.0 * (2 ** (self._fail_streak - 1))))
            else:
                self._fail_streak = 0
                try:
                    app_dm_sleep.tick(self, float(self._clock()))
                except Exception as exc:       # 醒來讀私聊失敗只記一笔，不影响事件循环
                    self.log(f"app adapter[{self.entity}] 醒來讀私聊失败 {type(exc).__name__}: {exc}")
                # 有 kick 就立刻醒（`wake()` 會讓 wait 馬上回），沒有就等到兜底對賬時間。
                # 群聊到期與敲門由回合工作线程自己按時醒（`_turn_loop`）。
                self._wake.wait(self._idle_s)
                self._wake.clear()

    def _turn_loop(self) -> None:
        """群聊回合工作线程（到期回合＋敲门）；本体在 `app_group_delay.run_worker`。"""
        app_group_delay.run_worker(self, knock=knock_tick)

    def close(self) -> None:
        self._stop.set()
        self._wake.set()                   # 叫停等待，執行緒才不會多睡一個兜底週期
        self._turn_wake.set()
        for name in ("_thread", "_turn_thread"):
            thread = getattr(self, name)
            if thread is not None:
                thread.join(timeout=3.0)
                setattr(self, name, None)


def build_app_chat_adapters(state, env=None, *, opener=None) -> list[AppChatAdapter]:
    env = os.environ if env is None else env
    base_url = str(env.get("BLUEAGENT_APP_SERVER_URL") or "").strip()
    tokens = _tokens(str(env.get("BLUEAGENT_APP_SERVER_TOKENS") or ""))
    if not base_url or not tokens:
        return []

    # ④ 点名优先要用各角色自己的叫名（id／显示名／nicknames），别拿全域唤醒词——
    # 全域表里有别人的名字，喊「ノリ」会把ミラ也算进「被点名的人」。
    aliases: dict[str, tuple[str, ...]] = {}
    registry = getattr(getattr(state, "app", None), "entities", None)
    for entity_id in tokens:
        words = [entity_id]
        found = registry.get(entity_id) if registry is not None else None
        if found is not None:
            words.extend([str(getattr(found, "name", "") or "")])
            words.extend(str(item) for item in (getattr(found, "nicknames", ()) or ()))
        aliases[entity_id] = tuple(dict.fromkeys(word for word in words if word))
    # 各自的生物鐘（owner 2026-11-08）：睡著的角色不「拿起手機」（@點名仍可叫醒）。
    circadian = getattr(getattr(state, "app", None), "circadian", None) or {}
    root = pathlib.Path(state.app.data_dir) / "app-adapter"
    ledger = GroupLedger(root / "group-ledger.json")      # 三人共用一本（M5a）
    world_events = getattr(state.app, "world_events", None)

    names = {entity.id: str(getattr(entity, "name", entity.id))
             for entity in (registry.all() if registry is not None else [])}

    def on_board(entity, kind, notice, _event):
        if world_events is None:
            return
        # 公共区回流：一条「谁对我的帖做了什么」的私有感知；不带帖 id 之外的原始 payload。
        world_events.publish("board", str(notice.get("text") or ""), entities=(entity,))

    def on_location(entity, event_type, payload):
        if world_events is None:
            return
        if event_type == "location.revoked":
            text = "位置共有が停止された"
        else:
            location = payload.get("location") or {}
            principal = str(location.get("principal") or "")
            text = "主人のいる場所が変わった" if principal == "owner" \
                else "共有された位置が変わった"
        # 私有感知只描述变化；坐标与原始 payload 不进入世界事件或聊天上下文。
        world_events.publish("location", text, entities=(entity,))

    app = getattr(state, "app", None)
    budget = getattr(app, "budget", None)
    activity_log = getattr(app, "activity_log", None)
    return [AppChatAdapter(entity, AppServerClient(base_url, token), state.send,
                           root / f"{entity}-cursor.json", state.reply_text, state.reply_bubbles,
                           log=state._log_command, group_ledger=ledger,
                           on_location=on_location, on_board=on_board, aliases=aliases,
                           names=names, circadian=circadian, opener=opener,
                           budget=budget, activity_log=activity_log)
            for entity, token in sorted(tokens.items())]


__all__ = ["AppChatAdapter", "AppServerClient", "AttentionDecision", "GroupAttention",
           "GroupLedger", "build_app_chat_adapters"]
