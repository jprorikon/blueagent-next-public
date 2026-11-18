"""HTTP/靜態層：只解析請求、寫回應；業務在 `api.py`。客戶端在 `client/`。"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler

HERE = pathlib.Path(__file__).resolve().parent
NEXTGEN = HERE.parent                      # agent/
CLIENT = NEXTGEN / "client"
_REPO = NEXTGEN.parent                     # 倉庫根：包名 agent.*
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from agent.brain.compose.state import AppState  # noqa: E402
from agent.server import api                            # noqa: E402
from agent.brain.media.stickers import MIME as STICKER_MIME  # noqa: E402
from agent.brain.media import digest                          # noqa: E402
from agent.brain.media import app_audio, thumbs               # noqa: E402
from agent.brain.domain.client_end import end_of               # noqa: E402

STATE: AppState | None = None


def set_state(state: AppState) -> None:
    global STATE
    STATE = state


from agent.server.sse_names import bus_event_name, sse_name


class Handler(BaseHTTPRequestHandler):
    server_version = "BlueAgentNext/0.1"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:                     # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            self._json(*api.health(self._state(), auth=self._auth()))
            return
        if path == "/api/events":
            auth = self._auth()
            if auth is None:
                self._json(401, {"error": "unauthorized",
                                 "hint": "Authorization: Bearer <token>"})
                return
            self._events(auth=auth)
            return
        if path == "/api/me":
            self._json(*api.get_me(self._state(), auth=self._auth()))
            return
        if path == "/api/accounts":
            self._json(*api.list_accounts(self._state(), auth=self._auth()))
            return
        if path == "/api/accounts/export":
            self._json(*api.export_accounts(self._state(), auth=self._auth()))
            return
        if path == "/api/turns":
            status, payload = api.list_turns(self._state(), urllib.parse.parse_qs(parsed.query),
                                             auth=self._auth())
            self._json(status, payload, etag=payload.get("digest", "") if status == 200 else "")
            return
        from agent.server import http_routes
        if http_routes.get(self, path, urllib.parse.parse_qs(parsed.query)):
            return
        if path.startswith("/api/turns/"):
            rest = path[len("/api/turns/"):].strip("/")
            if rest.endswith("/audio"):
                turn_id = rest[: -len("/audio")].strip("/")
                status, audio = api.get_turn_audio(self._state(), turn_id, auth=self._auth())
                if status != 200 or not audio:
                    self._json(status or 404, {"error": "audio_not_found"})
                    return
                audio = app_audio.transport_wav(audio, self.headers.get("X-Audio-Profile", ""))
                self._turn_audio(audio)
                return
            self._json(*api.get_turn(self._state(), rest, auth=self._auth()))
            return
        if path == "/api/memory/windows":
            self._json(*api.get_memory_windows(self._state(), urllib.parse.parse_qs(parsed.query),
                                               auth=self._auth()))
            return
        if path == "/api/memory/trace":
            self._json(*api.get_memory_trace(self._state(), urllib.parse.parse_qs(parsed.query),
                                             auth=self._auth()))
            return
        if path == "/api/self/next":
            self._json(*api.get_self_task(self._state(), urllib.parse.parse_qs(parsed.query),
                                          auth=self._auth()))
            return
        if path == "/api/trpg/tables":
            self._json(*api.get_trpg_tables(self._state(), urllib.parse.parse_qs(parsed.query),
                                            auth=self._auth()))
            return
        if path == "/api/trpg/table":
            self._json(*api.get_trpg_table(self._state(), urllib.parse.parse_qs(parsed.query),
                                           auth=self._auth()))
            return
        if path == "/api/stickers":
            status, payload = api.list_stickers(self._state(), urllib.parse.parse_qs(parsed.query),
                                                auth=self._auth())
            self._json(status, payload, etag=payload.get("version", "") if status == 200 else "")
            return
        if path.startswith("/api/stickers/"):
            self._sticker(path[len("/api/stickers/"):], urllib.parse.parse_qs(parsed.query))
            return
        if path == "/api/tasks":
            # owner 端看她自己的「看板／日曆」（owner 2026-11-03）
            status, payload = api.get_tasks(self._state(),
                                            urllib.parse.parse_qs(parsed.query),
                                            auth=self._auth())
            self._json(status, payload, etag=payload.get("digest", "") if status == 200 else "")
            return
        if path == "/api/diary" or path.startswith("/api/diary/"):
            self._diary(path, urllib.parse.parse_qs(parsed.query))
            return
        if path == "/api/motion":
            self._json(*api.list_motion(self._state(), urllib.parse.parse_qs(parsed.query),
                                        auth=self._auth()))
            return
        if path.startswith("/api/motion/"):
            clip_id = path[len("/api/motion/"):].strip("/")
            status, payload = api.get_motion(self._state(), clip_id, auth=self._auth())
            # 片段 id 同時是 ETag（§5.5）：端末取過就不重下載，304 不帶 body
            self._json(status, payload, etag=str(payload.get("id") or "") if status == 200 else "")
            return
        if path == "/api/media":
            self._json(*api.list_media(self._state(), urllib.parse.parse_qs(parsed.query),
                                       auth=self._auth()))
            return
        if path.startswith("/api/media/"):
            http_routes.media(self, path[len("/api/media/"):].strip("/"),
                              urllib.parse.parse_qs(parsed.query))
            return
        if path == "/api/mood":
            self._json(*api.get_mood(self._state(),
                                     urllib.parse.parse_qs(parsed.query),
                                     auth=self._auth()))
            return
        if path == "/api/state":
            self._json(*api.get_state(self._state(),
                                      urllib.parse.parse_qs(parsed.query),
                                      auth=self._auth()))
            return
        if path == "/api/guest/status":
            self._json(*api.get_guest_status(self._state(),
                                             urllib.parse.parse_qs(parsed.query)))
            return
        if path == "/api/guests/directory":
            self._json(*api.get_guest_directory(self._state(),
                                                urllib.parse.parse_qs(parsed.query),
                                                auth=self._auth()))
            return
        if path == "/api/owner/guests":
            self._json(*api.get_owner_guests(self._state(), auth=self._auth()))
            return
        if path == "/api/discord/whitelist":
            self._json(*api.get_whitelist(self._state(), auth=self._auth()))
            return
        if path == "/api/owner/stances":
            self._json(*api.get_owner_stances(self._state(),
                                              urllib.parse.parse_qs(parsed.query),
                                              auth=self._auth()))
            return
        if path == "/api/commands":
            self._json(*api.get_commands(self._state(),
                                         urllib.parse.parse_qs(parsed.query),
                                         auth=self._auth()))
            return
        if path in ("/", "/index.html"):
            self._file(CLIENT / "index.html")
            return
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            target = (CLIENT / rel).resolve()
            if not str(target).startswith(str(CLIENT.resolve())):
                self._json(403, {"error": "forbidden"})
                return
            self._file(target)
            return
        self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:                  # noqa: N802
        from agent.server import http_routes
        http_routes.delete(self, urllib.parse.urlparse(self.path).path)

    def do_POST(self) -> None:                    # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        auth = self._auth()
        ip = self._client_ip()
        source = str(self.headers.get("X-Source") or "")
        external_id = str(self.headers.get("X-External-Id") or "")
        # App Server 内部语音 pipe 使用独立共享密钥，不套用终端用户 token。
        if parsed.path == "/internal/app/voice":
            from agent.server import app_voice_http
            app_voice_http.handle(self)
            return
        # ⚠️ 音檔路由必須在 `_body()`（讀 JSON）之前：先解析 body 會把二進位當壞 JSON 打成 400。
        # `/api/media`＝只存圖、不起回合（延時聚合時先上傳拿 id），與 `/api/image` 共用讀檔那一段。
        if parsed.path in {"/api/image", "/api/media"}:
            self._image(auth=auth, ip=ip, source=source, external_id=external_id,
                        store_only=parsed.path == "/api/media")
            return
        if parsed.path == "/api/audio":
            self._audio(auth=auth, ip=ip, source=source, external_id=external_id)
            return
        try:
            body = self._body()
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        if parsed.path in {"/api/stream", "/api/turn"}:   # `/api/turn`＝契約名，`/api/stream` 保留相容
            self._stream(body, auth=auth, ip=ip, source=source,
                         external_id=external_id)
            return
        if parsed.path == "/trpg/kick":
            self._trpg_kick(body)
            return
        if parsed.path in {"/api/app/kick", "/api/app/introspect"}:   # 驗 token＋分發：server/api/app_kick.py
            self._json(*api.app_kick_handle(self._state(), self.headers.get("X-App-Kick-Token"), body, parsed.path))
            return
        self._json(*api.dispatch_post(
            self._state(), parsed.path, body, auth=auth, ip=ip,
            source=source, external_id=external_id,
        ))

    def _image(self, *, auth, ip: str, source: str, external_id: str,
               store_only: bool = False) -> None:
        """`POST /api/image`：body 是圖檔（png/jpg/webp），回應 SSE（第一幀 `image`，之後與打字同一條）。"""
        # 二進位入口必須在讀 body 前擋人、擋大包：後面的 401／413 再正確也拿不回已配置的記憶體。
        if not getattr(auth, "principal", ""):
            self._json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0:
            self._json(400, {"error": "image required"})
            return
        limit = api.store_max_bytes()
        if length > limit:
            self._json(413, {"error": "image_too_large", "limit": limit})
            return
        data = self.rfile.read(length)
        headers = {key: self.headers.get(key, "") for key in
                   ("Content-Type", "X-Entity", "X-Mode", "X-Request-Id", "X-Note",
                    "X-Name", "X-Want-Voice")}
        status, payload = api.upload_or_prepare_image(
            self._state(), data, headers=headers, auth=auth, ip=ip,
            source=source, external_id=external_id, store_only=store_only)
        if status != 200:
            self._json(status, payload)
            return
        if store_only:
            self._json(200, payload)
            return
        # ⚠️ 只把「端末要看的」那幾個欄位當 SSE 首幀；`payload["media"]`（含 path／mime）
        #    **必須留著**給 `send_stream` —— VisionStage 就是靠它讀圖（從前這裡把它 pop 掉，
        #    於是每一張圖都變成「読めませんでした」，2026-10-27）。
        media = dict(payload.pop("media_public", {}) or {})
        payload.pop("input", None)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self._event("image", media)
            for kind, frame in self._state().send_stream(**payload):
                self._event(*sse_name(kind, frame))
        except (BrokenPipeError, ConnectionResetError):
            return

    def _sticker(self, rest: str, query: dict | None = None) -> None:
        """`GET /api/stickers/<entity>/<id>[?w=192]`：表情包的圖（兩端都能拿；找不到一律 404）。"""
        entity, _, sticker_id = rest.strip("/").partition("/")
        status, target = api.sticker_file(self._state(), entity, sticker_id, auth=self._auth())
        if target is None:
            self._json(status, {"error": {401: "unauthorized", 403: "forbidden"}.get(
                status, "sticker_not_found")})
            return
        self._picture(target, STICKER_MIME.get(target.suffix.lower(), "application/octet-stream"), query)

    def _diary(self, path: str, query: dict) -> None:
        """`/api/diary`（索引）、`/api/diary/day`（某一天的筆記）、`/api/diary/work`（成品全文）。

        前兩支帶 `digest` ⇒ 端末可以帶 `If-None-Match` 問 304（日記頁有本機快取）。
        """
        if path == "/api/diary/work":
            self._json(*api.get_diary_work(self._state(), query, auth=self._auth()))
            return
        reader = api.get_diary_day if path == "/api/diary/day" else api.get_diary
        status, payload = reader(self._state(), query, auth=self._auth())
        self._json(status, payload, etag=payload.get("digest", "") if status == 200 else "")

    def _picture(self, target: pathlib.Path, mime: str, query: dict | None) -> None:
        """回一張圖；帶 `?w=` 就回縮圖（ETag＝原圖雜湊＋寬，端末拿清單的雜湊就推得出來）。"""
        data = target.read_bytes()
        width = thumbs.pick_width(((query or {}).get("w") or [""])[0])
        if width:
            small = thumbs.thumbnail(target, width, thumbs.cache_dir())
            if small is not None:
                thumb_mime = "image/png" if small.suffix == ".png" else "image/jpeg"
                self._bytes(small.read_bytes(), thumb_mime,
                            tag=thumbs.thumb_tag(digest.content_tag(data), width))
                return
            # 做不出縮圖（沒有 ffmpeg）⇒ 給原圖，但 ETag 照縮圖那樣標：端末用清單推的雜湊照樣對得上
            self._bytes(data, mime, tag=thumbs.thumb_tag(digest.content_tag(data), width))
            return
        self._bytes(data, mime)

    def _bytes(self, data: bytes, mime: str, *, tag: str = "") -> None:
        """回一個檔，附內容雜湊當 ETag；端末帶著同一個雜湊來問 ⇒ 304、不重傳（owner 2026-10-29 快取）。"""
        tag = tag or digest.content_tag(data)
        if self._not_modified(tag):
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", f'"{tag}"')
        # `no-cache`＝「可以存，但用之前要先來問」：快取由端末自己管，平台的 HTTP 快取不准自作主張沿用
        self.send_header("Cache-Control", "private, no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _turn_audio(self, audio: bytes) -> None:
        from agent.server.http_responses import turn_audio
        turn_audio(self, audio)

    def _not_modified(self, tag: str, *, weak: bool = False, vary: bool = False) -> bool:
        """`If-None-Match` 對上了就回 304（沒有本文）並回 True。"""
        if not digest.matches(self.headers.get("If-None-Match", ""), tag):
            return False
        self.send_response(304)
        self.send_header("ETag", f'{"W/" if weak else ""}"{tag}"')
        self.send_header("Cache-Control", "private, no-cache")
        if vary:
            self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        return True

    def _audio(self, *, auth, ip: str, source: str, external_id: str) -> None:
        """`POST /api/audio`：body 是音檔（wav 或裸 PCM16），回應 SSE，第一幀是 `transcript`。"""
        if not getattr(auth, "principal", ""):
            self._json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0:
            self._json(400, {"error": "audio required"})
            return
        if length > 12_000_000:
            self._json(413, {"error": "audio too large"})
            return
        audio = self.rfile.read(length)
        headers = {key: self.headers.get(key, "") for key in
                   ("X-Lang", "X-Rate", "X-Entity", "X-Mode", "X-Request-Id",
                    "X-Want-Voice", "X-Call")}
        status, data = api.prepare_audio(
            self._state(), audio, headers=headers, auth=auth, ip=ip,
            source=source, external_id=external_id)
        if status != 200:
            self._json(status, data)
            return
        if data.get("merged"):
            # 通話裡這句沒講完、已經併進主人接著說的下一句 ⇒ 這條線只回報轉寫就收
            # （App 那邊多半已經因為主人又開口而放掉這條線了）
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self._event("transcript", dict(data.get("transcript") or {}))
                self._event("merged", {"call": str(data.get("call") or "")})
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        # `input` / `transcript` 只是回報用的，`send_stream` 不收這兩個鍵
        transcript = dict(data.pop("transcript") or {})
        data.pop("input", None)
        in_call = bool(data.pop("call", ""))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self._event("transcript", transcript)
            # 通話中不拆「先短回、後交付」：後續交付只推文字，電話那頭聽不到；
            # 整輪直接串流（首句優先合成），聲音全在這條線上。
            if str(data.get("mode") or "") == "work" and not in_call:
                # 語音 + 要幹活：先短回一句（這裡回完），真正的工作排隊做完再由
                # `GET /api/events` 推送 `delivery` —— App 必須常駐那條長連才收得到。
                result = self._state().send_voice(**data)
                for frame in result.get("frames") or []:
                    self._event(str(frame.get("kind") or "frame"), dict(frame))
                self._event("result", {"following_up": bool(result.get("following_up")),
                                       "turn": str((result.get("decision") or {}).get("thread") or "")})
                return
            for kind, payload in self._state().send_stream(**data):
                self._event(*sse_name(kind, payload))
        except (BrokenPipeError, ConnectionResetError):
            return

    def _event(self, event: str, payload: dict, *, stream_profile: str = "",
               audio_profile: str = "") -> None:
        # 只動送給 App 的這一份；原始 frame 留給 outbox／Discord。SSE 本身不 gzip。
        from agent.server.stream_profile import slim_done_event
        headers = getattr(self, "headers", {})
        audio_profile = audio_profile or headers.get("X-Audio-Profile", "")
        stream_profile = stream_profile or headers.get("X-Stream-Profile", "")
        payload = app_audio.slim_speech_event(event, payload, audio_profile)
        payload = slim_done_event(event, payload, stream_profile)
        chunk = (f"event: {event}\n"
                 f"data: {json.dumps(payload, ensure_ascii=False)}\n\n")
        self.wfile.write(chunk.encode("utf-8"))
        self.wfile.flush()

    def _events(self, *, auth) -> None:
        """`GET /api/events`：**App 的常駐 SSE**。

        用途：語音回合的「後續交付」「主動訊息」沒有請求可以回，只能推。
        契約（詳見 `docs/APP-API-SSE.md`）：
          · `event: delivery` 一條完整回覆（`text`、`entity`、`thread`）
          · `event: mood` 主人的情緒快照（`entity`、`mood`）
          · `: ping` 每 15 秒（連線還活著）
          · 每條訊息帶 `id:`；**重連帶 `Last-Event-ID`（或 `?since=`）⇒ 補上斷線期間漏的**
        """
        bus = getattr(self._state().app, "bus", None)
        if bus is None:
            self._json(503, {"error": "bus_unavailable"})
            return
        # 哪一端（`X-Source: mr`／`?end=mr`＝MR 頭顯；沒帶＝手機）：只收送到這一端的交付（owner 2026-11-01）
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        end = end_of(str(self.headers.get("X-Source") or (query.get("end") or [""])[0]))
        sub_id, channel = bus.subscribe(auth.principal, end)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(("event: ready\n"
                              f"data: {json.dumps({'principal': auth.principal}, ensure_ascii=False)}\n\n"
                              ).encode("utf-8"))
            self.wfile.flush()
            # 契約 v2：重連補洞。`Last-Event-ID` 是 SSE 標準標頭，也接受 `?since=`
            missed = bus.replay(auth.principal, self._last_event_id(), end)
            # 訂閱早於 replay：中間新發的事件會同時落在 history 與即時佇列。
            # 這個集合最多 history_max 筆，只過濾本次連線已補過的即時副本，不影響後續新事件。
            replayed = {str(envelope.get("id") or "") for envelope in missed}
            for envelope in missed:
                chunk = (f"id: {envelope.get('id')}\n"
                         f"event: {bus_event_name(envelope)}\n"
                         f"data: {json.dumps(envelope, ensure_ascii=False)}\n\n")
                self.wfile.write(chunk.encode("utf-8"))
            if missed:
                self.wfile.flush()
            while True:
                try:
                    envelope = channel.get(timeout=15.0)
                except Exception:                       # noqa: BLE001 - 佇列空 ⇒ 發心跳
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if str(envelope.get("id") or "") in replayed:
                    continue
                chunk = (f"id: {envelope.get('id')}\n"
                         f"event: {bus_event_name(envelope)}\n"
                         f"data: {json.dumps(envelope, ensure_ascii=False)}\n\n")
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            bus.unsubscribe(auth.principal, sub_id)
            self.close_connection = True

    def _last_event_id(self) -> int:
        """重連帶的游標：`Last-Event-ID` 標頭優先，其次 `?since=`／`?last_event_id=`。"""
        raw = str(self.headers.get("Last-Event-ID") or "").strip()
        if not raw:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            raw = str((query.get("since") or query.get("last_event_id") or [""])[0]).strip()
        try:
            return int(raw or 0)
        except ValueError:
            return 0

    def _trpg_kick(self, body: dict) -> None:
        """`POST /trpg/kick`：桌端叫醒她（`X-Table-Kick-Token` 驗身分）。

        **先回 200 再跑**：桌端的 kick 有 timeout（現役 10 s），而「她看牌面 → 模型 → TTS → 出手」
        常常超過 10 s。回得慢會被記成 kick 失敗、下一輪重踢（同一手做兩次）。
        `TrpgSeat` 自己有鎖，重送的 kick 會被判 `busy` 丢掉。
        """
        import threading

        from agent.server.integrations.trpg import pick_seat   # 一桌多席：按 kick 帶的席位分派
        seat, unknown = pick_seat(self._state().app, body)
        if seat is None:
            self._json(503, {"ok": False, "error": "trpg_disabled"})
            return
        expect = str(getattr(seat.config, "kick_token", "") or "")
        got = str(self.headers.get("X-Table-Kick-Token") or "")
        if not expect:
            self._json(503, {"ok": False, "error": "kick_token_not_configured"})
            return
        import hmac
        if not got or not hmac.compare_digest(got, expect):
            self._json(403, {"ok": False, "error": "bad token"})
            return
        if unknown:
            self._json(404, {"ok": False, "error": "unknown_seat", "seat": unknown})
            return
        threading.Thread(target=seat.handle_kick, args=(dict(body or {}),),
                         daemon=True, name="trpg-kick").start()
        self._json(200, {"ok": True, "taken": True})

    def _stream(self, body: dict, *, auth, ip: str, source: str,
                external_id: str) -> None:
        status, data = api.prepare_send(
            self._state(), body, auth=auth, ip=ip, source=source,
            external_id=external_id)
        if status != 200:
            self._json(status, data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            stream_profile = self.headers.get("X-Stream-Profile", "")
            audio_profile = self.headers.get("X-Audio-Profile", "")
            for kind, payload in self._state().send_stream(**data):
                self._event(*sse_name(kind, payload), stream_profile=stream_profile,
                            audio_profile=audio_profile)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.close_connection = True

    # ── 傳輸 ────────────────────────────────────────────────────────────────────
    def _state(self) -> AppState:
        if STATE is None:
            raise RuntimeError("state is not ready")
        return STATE

    def _auth(self):
        parsed = urllib.parse.urlparse(self.path)
        return self._state().app.auth.authenticate(
            self.headers.get("Authorization", ""), path=parsed.path,
            ip=self._client_ip())

    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else ""

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length > 1_000_000:
            raise ValueError("body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"bad json: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("json object required")
        return data

    def _json(self, status: int, payload: dict, *, etag: str = "") -> None:
        from agent.server.http_responses import json_response
        json_response(self, status, payload, etag=etag)

    def _file(self, path: pathlib.Path) -> None:
        from agent.server.http_responses import file_response
        file_response(self, path)

    def log_message(self, fmt: str, *args) -> None:   # noqa: A003
        sys.stderr.write("[ba-next] " + (fmt % args) + "\n")
