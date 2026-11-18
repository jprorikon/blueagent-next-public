"""独立 App Server HTTP 入口：健康检查、账号、MCP、事件同步与 SSE。"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.server import board_http, mcp
from app.server.accounts import Account
from app.server.config import AppConfig
from app.server.media import MAX_BYTES, _matches_etag
from app.server.services import AppServices
from app.server.social.models import BoardError

_MAX_BODY = 1_000_000
#: 常駐 SSE 每隔多久重驗一次 token（多設備配對，2026-11-16）：端末被撤銷後，已經開著的那條線
#: 也要斷——不然丟掉的手機會一直收到聊天，直到它自己斷線。加上 Agent 詢問的 60 秒快取，最慢約 90 秒。
SSE_REAUTH_S = 30.0
_CHAT_MESSAGE = re.compile(r"/v1/conversations/(?P<cid>[^/]+)/messages/(?P<mid>[^/]+)/?")


class AppHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, services: AppServices):
        self.services = services
        super().__init__(address, AppHandler)

    def server_close(self):
        self.services.close()
        super().server_close()


class AppHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def services(self) -> AppServices:
        return self.server.services

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _account(self) -> Account | None:
        account = self.services.accounts.authenticate(self.headers.get("Authorization", ""))
        if account is None:
            self._json(401, {"error": "unauthorized"})
        return account

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise ValueError("bad_content_length") from exc
        if length < 0 or length > _MAX_BODY:
            raise ValueError("body_too_large")
        payload = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("bad_request")
        return payload

    def _media_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise BoardError(400, "bad_content_length") from exc
        if length < 0:
            raise BoardError(400, "bad_content_length")
        if length > MAX_BYTES:
            # 分块丢弃而不分配巨型 buffer；读完再回 413，避免客户端只看见 BrokenPipe。
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    raise BoardError(400, "incomplete_body")
                remaining -= len(chunk)
            raise BoardError(413, "image_too_large", limit=MAX_BYTES)
        data = self.rfile.read(length)
        if len(data) != length:
            raise BoardError(400, "incomplete_body")
        return data

    def _picture(self, account: Account, media_id: str, query: dict) -> None:
        try:
            width = int((query.get("w") or ["0"])[0])
        except ValueError:
            width = 0
        _metadata, data, mime, tag = self.services.media.read(account.actor(), media_id, width=width)
        not_modified = _matches_etag(self.headers.get("If-None-Match", ""), tag)
        self.send_response(304 if not_modified else 200)
        self.send_header("ETag", f'"{tag}"')
        self.send_header("Cache-Control", "private, no-cache")
        self.send_header("Content-Length", "0" if not_modified else str(len(data)))
        if not not_modified:
            self.send_header("Content-Type", mime)
        self.end_headers()
        if not not_modified:
            self.wfile.write(data)

    def _sticker_list(self, account: Account, query: dict) -> None:
        entity = str((query.get("entity") or [account.id if account.kind == "entity" else "mira"])[0])
        payload = self.services.stickers.list(account.actor(), entity)
        tag = payload["version"]
        not_modified = _matches_etag(self.headers.get("If-None-Match", ""), tag)
        if not_modified:
            self.send_response(304)
            self.send_header("ETag", f'"{tag}"')
            self.send_header("Cache-Control", "private, no-cache")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("ETag", f'"{tag}"')
        self.send_header("Cache-Control", "private, no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sticker_file(self, account: Account, path: str, query: dict) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != 2:
            raise BoardError(404, "not_found")
        try:
            width = int((query.get("w") or ["0"])[0])
        except ValueError:
            width = 0
        data, mime, tag = self.services.stickers.read(
            account.actor(), parts[0], parts[1], width=width)
        not_modified = _matches_etag(self.headers.get("If-None-Match", ""), tag)
        self.send_response(304 if not_modified else 200)
        self.send_header("ETag", f'"{tag}"')
        self.send_header("Cache-Control", "private, no-cache")
        self.send_header("Content-Length", "0" if not_modified else str(len(data)))
        if not not_modified:
            self.send_header("Content-Type", mime)
        self.end_headers()
        if not not_modified:
            self.wfile.write(data)

    def _event_cursor(self, query: dict) -> str:
        event_id = str(self.headers.get("Last-Event-ID") or "")
        if event_id:
            return self.services.events.cursor_for_event(event_id)
        return self.services.events.normalize_cursor((query.get("after") or ["0"])[0])

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/health":
            self._json(200, {"ok": True, "service": "blueagent-app-server", "schema_version": 1})
            return
        account = self._account()
        if account is None:
            return
        try:
            if parsed.path == "/v1/me":
                self._json(200, {"account": account.public()})
                return
            if parsed.path == "/v1/me/mood":
                self._json(200, self.services.mood.get(account.actor()))
                return
            if parsed.path == "/v1/me/location":
                self._json(200, self.services.location.status(account.actor()))
                return
            if parsed.path == "/v1/moods":
                self._json(200, self.services.mood.list(account.actor()))
                return
            board = board_http.get(self.services, account.actor(), parsed.path, query)
            if board is not None:
                self._json(200, board)
                return
            if parsed.path == "/v1/media":
                self._json(200, self.services.media.list(
                    account.actor(), offset=int((query.get("offset") or ["0"])[0]),
                    limit=int((query.get("limit") or ["30"])[0])))
                return
            if parsed.path.startswith("/v1/media/"):
                media_id = parsed.path[len("/v1/media/"):].strip("/")
                self._picture(account, media_id, query)
                return
            if parsed.path == "/v1/stickers":
                self._sticker_list(account, query)
                return
            if parsed.path.startswith("/v1/stickers/"):
                self._sticker_file(account, parsed.path[len("/v1/stickers/"):], query)
                return
            if parsed.path == "/v1/conversations":
                self._json(200, self.services.chat.list(account.actor()))
                return
            message_path = parsed.path.startswith("/v1/conversations/") \
                and parsed.path.endswith("/messages")
            if message_path:
                conversation_id = parsed.path[len("/v1/conversations/"):-len("/messages")].strip("/")
                self._json(200, self.services.chat.messages(
                    account.actor(), conversation_id,
                    before=str((query.get("before") or [""])[0]),
                    after=str((query.get("after") or [""])[0]),
                    limit=int((query.get("limit") or ["50"])[0])))
                return
            if parsed.path == "/v1/calls/incoming":
                # 她打給主人、此刻還在響的那一通（App 回前台／點系統通知進來時問）
                self._json(200, self.services.voice.incoming(account))
                return
            call_events = parsed.path.startswith("/v1/calls/") and parsed.path.endswith("/events")
            # 兩階段響鈴（2026-11-17）：響鈴期間客戶端**輪詢**這一支問「她接了嗎」。
            call_state = parsed.path.startswith("/v1/calls/") and parsed.path.endswith("/state")
            if call_state:
                call_id = parsed.path[len("/v1/calls/"):-len("/state")].strip("/")
                self._json(200, self.services.voice.state(account, call_id))
                return
            if call_events:
                call_id = parsed.path[len("/v1/calls/"):-len("/events")].strip("/")
                cursor = int(self.headers.get("Last-Event-ID") or (query.get("after") or ["0"])[0])
                if "text/event-stream" in self.headers.get("Accept", ""):
                    self._call_sse(account, call_id, cursor)
                else:
                    self._json(200, self.services.voice.events(account, call_id, cursor))
                return
            if parsed.path == "/v1/events":
                cursor = self._event_cursor(query)
                if "text/event-stream" in self.headers.get("Accept", ""):
                    self._sse(cursor, account.id)
                else:
                    limit = int((query.get("limit") or ["100"])[0])
                    self._json(200, self.services.events.list(
                        after=cursor, limit=limit, account_id=account.id))
                return
            self._json(404, {"error": "not_found"})
        except BoardError as exc:
            self._json(exc.status, exc.payload())
        except ValueError as exc:
            code = str(exc) if str(exc) in {"bad_cursor", "cursor_expired"} else "bad_request"
            status = 409 if code == "cursor_expired" else 400
            self._json(status, {"error": code, "resync_required": code == "cursor_expired"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        path = urllib.parse.urlsplit(self.path).path
        account = self._account()
        if account is None:
            return
        try:
            if path == "/mcp":
                reply = mcp.handle(self._body(), self.services.board, account.actor(),
                                   chat=self.services.chat, media=self.services.media,
                                   accounts=self.services.accounts, mood=self.services.mood,
                                   location=self.services.location, stickers=self.services.stickers)
                if reply is None:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._json(200, reply)
                return
            if path == "/v1/calls":
                body = self._body()
                self._json(200, self.services.voice.open(
                    account, str(body.get("entity") or ""), str(body.get("language") or "")))
                return
            if path == "/v1/calls/outgoing":
                # 她打給主人（實體帳號用自己的 token）：建一通響鈴中的來電，見 `voice_outgoing.py`
                body = self._body()
                self._json(200, self.services.voice.dial(
                    account, str(body.get("to") or ""), str(body.get("topic") or ""),
                    str(body.get("language") or "")))
                return
            call_action = path.startswith("/v1/calls/") and path.rsplit("/", 1)[-1] in {
                "frames", "ping", "interrupt", "hangup", "accept", "decline"}
            if call_action:
                call_id, action = path[len("/v1/calls/"):].rsplit("/", 1)
                body = self._body()
                if action == "frames":
                    # 2026-11-08：手機可以**一次帶多幀**（`frames: [{seq,audio},…]`）。
                    # 為什麼：一幀一請求在 4G／Wi-Fi 抖動下追不上 10 幀/秒，積壓就被客戶端
                    # 50 幀保險絲掛斷（真實事故）。批量後請求數掉到 1/4，服務端照舊逐幀走同一條路。
                    batched = body.get("frames") if isinstance(body, dict) else None
                    if isinstance(batched, list) and batched:
                        reply = None
                        for item in batched:
                            if not isinstance(item, dict):
                                raise BoardError(400, "bad_audio_frame")
                            reply = self.services.voice.feed(
                                account, call_id, item.get("seq"), item.get("audio"))
                        self._json(200, reply or {"ok": True})
                    else:
                        self._json(200, self.services.voice.feed(
                            account, call_id, body.get("seq"), body.get("audio")))
                elif action == "ping":
                    self._json(200, self.services.voice.ping(account, call_id))
                elif action == "interrupt":
                    self._json(200, self.services.voice.interrupt(account, call_id))
                elif action == "accept":
                    self._json(200, self.services.voice.accept(account, call_id))
                elif action == "decline":
                    self._json(200, self.services.voice.decline(account, call_id))
                else:
                    self._json(200, self.services.voice.hangup(account, call_id))
                return
            if path == "/v1/voice/status":
                self._json(200, self.services.voice.tts_status())
                return
            if path == "/v1/me/location":
                self._json(200, self.services.location.report(account.actor(), self._body()))
                return
            if board_http.owns(path):
                board = board_http.post(self.services, account.actor(), path, self._body())
                if board is not None:
                    self._json(200, board)
                    return
            if path == "/v1/media":
                item = self.services.media.upload(
                    account.actor(), self._media_body(), self.headers.get("Content-Type", ""),
                    name=urllib.parse.unquote(self.headers.get("X-Name", "")))
                self._json(200, item)
                return
            if path == "/v1/conversations/direct":
                body = self._body()
                conversation = self.services.chat.open_direct(
                    account.actor(), str(body.get("account_id") or body.get("entity") or ""))
                self._json(200, {"conversation": conversation})
                return
            if path == "/v1/conversations/agent-room":
                self._json(200, {"conversation": self.services.chat.open_agent_room(account.actor())})
                return
            if path.startswith("/v1/conversations/") and path.endswith("/messages"):
                conversation_id = path[len("/v1/conversations/"):-len("/messages")].strip("/")
                body = self._body()
                self._json(200, self.services.chat.send(
                    account.actor(), conversation_id, text=body.get("text"), parts=body.get("parts"),
                    client_mutation_id=str(body.get("client_mutation_id") or ""),
                    causation_id=str(body.get("causation_id") or "")))
                return
            if path.startswith("/v1/conversations/") and path.endswith("/read"):
                conversation_id = path[len("/v1/conversations/"):-len("/read")].strip("/")
                body = self._body()
                self._json(200, {"conversation": self.services.chat.mark_read(
                    account.actor(), conversation_id, str(body.get("message_id") or ""))})
                return
            self._json(404, {"error": "not_found"})
        except BoardError as exc:
            self._json(exc.status, exc.payload())
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "bad_request"})
        finally:
            # 推送叫醒（`PLAN-app-kick-20261108.md`）：業務事務已提交才叫，Agent 立刻拉得到。
            # 失敗只記日誌、不影響這次回應；`BLUEAGENT_APP_KICK_URL` 留空＝功能關。
            self.services.events.flush()

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        path = urllib.parse.urlsplit(self.path).path
        account = self._account()
        if account is None:
            return
        try:
            # 私聊／群聊刪自己的一則（2026-11-16）：/v1/conversations/{id}/messages/{message_id}
            chat = _CHAT_MESSAGE.fullmatch(path)
            if chat is not None:
                self._json(200, self.services.chat.delete(account.actor(), chat["cid"], chat["mid"]))
                return
            board = board_http.delete(self.services, account.actor(), path)
            self._json(*((200, board) if board is not None else (404, {"error": "not_found"})))
        except BoardError as exc:
            self._json(exc.status, exc.payload())
        finally:
            self.services.events.flush()

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        path = urllib.parse.urlsplit(self.path).path
        account = self._account()
        if account is None:
            return
        try:
            if path == "/v1/me/mood":
                if account.kind == "entity":
                    raise BoardError(403, "mood_use_mcp")
                body = self._body()
                self._json(200, self.services.mood.set(
                    account.actor(), body.get("key"), body.get("note") or ""))
                return
            if path == "/v1/me/location":
                body = self._body()
                self._json(200, self.services.location.set_sharing(
                    account.actor(), body.get("enabled")))
                return
            self._json(404, {"error": "not_found"})
        except BoardError as exc:
            self._json(exc.status, exc.payload())
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "bad_request"})
        finally:
            # 心情／位置是「一般公共事件，定期讀」（owner 2026-11-08）⇒ 不會叫醒任何人，
            # 但還是要清掉這條連線上累積的待叫佇列，免得漏到同一個 keep-alive 執行緒的下一個請求。
            self.services.events.flush()

    def _call_sse(self, account: Account, call_id: str, cursor: int) -> None:
        # 先取一次，让过期游标在写 SSE 响应头之前得到明确的 409。
        result = self.services.voice.events(account, call_id, cursor)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.wfile.write(b"retry: 1000\n\nevent: ready\ndata: {}\n\n")
        self.wfile.flush()
        heartbeat = time.monotonic()
        try:
            while not self.services.stopping.is_set():
                for event in result["events"]:
                    # 数据内也带游标：现有 Unity SSE 分发器不暴露 id，但仍能精确重连。
                    data = json.dumps({**event["payload"], "_seq": event["seq"]}, ensure_ascii=False)
                    frame = f"id: {event['seq']}\nevent: {event['name']}\ndata: {data}\n\n"
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                cursor = result["next_cursor"]
                if result["state"] != "live" and not result["events"]:
                    self.services.voice.log(f"app voice SSE 收尾：{call_id} state={result['state']}")
                    return
                now = time.monotonic()
                if now - heartbeat >= 15:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    heartbeat = now
                result = self.services.voice.events(account, call_id, cursor, wait=0.5)
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            self.close_connection = True

    def _sse(self, cursor: str, account_id: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        # 与旧客户端同一控制帧语义：只有收到 ready 才展示 online；不带 id，不推进事件游标。
        self.wfile.write(b"retry: 3000\n\nevent: ready\ndata: {}\n\n")
        self.wfile.flush()
        heartbeat = time.monotonic()
        checked = heartbeat
        try:
            while not self.services.stopping.is_set():
                if time.monotonic() - checked >= SSE_REAUTH_S:
                    checked = time.monotonic()
                    still = self.services.accounts.authenticate(self.headers.get("Authorization", ""))
                    if still is None or still.id != account_id:
                        return                 # 收線：端末重連時會拿到 401，自己回登入頁／停掉背景通知
                result = self.services.events.list(after=cursor, limit=100, account_id=account_id)
                for event in result["events"]:
                    data = json.dumps(event, ensure_ascii=False)
                    frame = f"id: {event['event_id']}\nevent: {event['type']}\ndata: {data}\n\n"
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                cursor = result["next_cursor"]
                now = time.monotonic()
                if now - heartbeat >= 15:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    heartbeat = now
                if not result["events"]:
                    self.services.stopping.wait(0.5)
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            self.close_connection = True


def make_server(config: AppConfig) -> AppHTTPServer:
    services = AppServices(config)
    try:
        return AppHTTPServer((config.host, config.port), services)
    except Exception:
        services.close()
        raise


__all__ = ["AppHandler", "AppHTTPServer", "make_server"]
