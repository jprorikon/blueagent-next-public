"""對話回合：送出（文字／音檔）、outbox 補拿、位置。"""

from __future__ import annotations

import time
import traceback
from agent.brain.compose import voice_api
from agent.brain.compose.state import AppState
from agent.brain.domain.auth import AuthContext, Role
from agent.brain.domain.client_end import MR, call_source, end_of
from agent.brain.media.digest import json_tag

from agent.server.api.common import _forbidden, _id, _participants, _unauthorized


def list_turns(state: AppState, query: dict, *, auth=None) -> tuple[int, dict]:
    """`GET /api/turns?since=<turn_id>&limit=N`：斷線後補拿（只看得到自己的）。

    App 收到 `accepted{turn_id}` 後若斷線，重連時帶最後一個**完整收到**的 turn_id，
    這裡就回之後那幾輪的文字（有音檔的會標 `audio: true`，用 `/api/turns/<id>/audio` 取）。
    """
    outbox = getattr(state.app, "outbox", None)
    if outbox is None:
        return 503, {"error": "outbox_unavailable"}
    principal = getattr(auth, "principal", "") or ""
    if not principal:
        return 401, {"error": "unauthorized"}
    since = str((query.get("since") or [""])[0] or "")
    try:
        limit = max(1, min(200, int((query.get("limit") or ["50"])[0])))
    except (TypeError, ValueError):
        limit = 50
    entity = str((query.get("entity") or [""])[0] or "").strip()
    thread = str((query.get("thread") or [""])[0] or "").strip()
    before = str((query.get("before") or [""])[0] or "").strip()
    # `source`＝來源端前綴，可用逗號給多個（`phone,api` → App 這一端的回合；
    # `discord` → 只回 Discord 的）。owner 2026-10-27：**App 與 Discord 的對話紀錄各自獨立**。
    # ⚠️ App 自己的回合有兩種標記：打字／語音是 `phone:<principal>`，圖片是 `api:image`
    #    （`server/api/media.py` 的 `source`）——只給 `phone` 會把他的圖片那輪濾掉（實測踩到）。
    source_raw = str((query.get("source") or [""])[0] or "").strip()
    sources = [part.strip() for part in source_raw.split(",") if part.strip()]
    # 历史/会话视图要「按角色或会话取一段」——旧壳是本地 history.jsonl，新仓以服务端为源，
    # 所以这里给同一个端点加三个过滤参数（`entity`／`thread`／`before`），返回形状不变。
    # ⚠️ 过滤参数与 `since` 可以并存：端末换角色后补拿会同时带 `entity` + `since`，
    #    两者的语意是「这一角色里、游标之后的那几轮」（2026-10-27 之前 `since` 在这一支被丢掉，
    #    于是补拿会跨角色倒进画面）。
    if entity or thread or before or sources:
        window = outbox.since("", principal=principal, limit=200)
        # 通話內容不進 App 的對話紀錄（owner 2026-10-29「通話內容不應該表示在 app 的聊天記錄
        # 裡面，可以只留一個簡單的標記」）⇒ `kind="call"` 一律濾掉；
        # 掛斷時寫的那條 `kind="call-log"` 標記留著（「發生過通話」）。
        window = [row for row in window if str(row.get("kind") or "") != "call"]
        if entity:
            window = [row for row in window if str(row.get("entity")) == entity]
        if thread:
            window = [row for row in window if str(row.get("thread")) == thread]
        if sources:
            # 前綴比對：`phone` 命中 `phone:owner`／`phone:guest-x`。
            # ⚠️ 2026-10-27 之前的列**沒有** `source` 欄位（raw 裡的 turn id 是 `t_<hex>`、
            #    outbox 的是 `t<毫秒>-<序>`，兩邊對不上，無法精確回填）⇒ 那些舊列在帶 `source`
            #    查詢時看不到，等 outbox 的 400 輪窗口自然淘汰。
            window = [row for row in window
                      if any(str(row.get("source") or "").startswith(prefix)
                             for prefix in sources)]
        if before:
            cut = next((pos for pos, row in enumerate(window)
                        if str(row.get("turn_id")) == before), None)
            if cut is not None:
                window = window[:cut]
            window = window[-limit:]
        elif since:
            start = next((pos for pos, row in enumerate(window)
                          if str(row.get("turn_id")) == since), None)
            # 游标还在窗口里 ⇒ 只回它之后的；找不到（超出 200 笔窗口）⇒ 就当没有游标，回最近几笔
            if start is not None:
                window = window[start + 1:]
            window = window[-limit:]
        else:
            window = window[-limit:]
        rows = window
    else:
        rows = outbox.since(since, principal=principal, limit=limit)
    # 保險：走哪一支都不回通話內容（通話內容不上 App 的對話紀錄）。
    rows = [row for row in rows if str(row.get("kind") or "") != "call"]
    # `digest`＝這一窗回合的雜湊（HTTP 層拿它當 ETag）：端末本機存著上次那一份，帶 `If-None-Match`
    # 來問，沒變就 304（owner 2026-10-29「對比線上的雜湊決定要不要重新同步」）。
    # 只算 `turns`：`stats` 每一輪都在變，算進去就永遠對不上。
    stats = (outbox.snapshot() if getattr(auth, "is_owner", False)
             else outbox.snapshot(principal=principal))
    return 200, {"since": since, "count": len(rows), "turns": rows, "digest": json_tag(rows),
                 "stats": stats}


def get_turn(state: AppState, turn_id: str, *, auth=None) -> tuple[int, dict]:
    outbox = getattr(state.app, "outbox", None)
    if outbox is None:
        return 503, {"error": "outbox_unavailable"}
    principal = getattr(auth, "principal", "") or ""
    if not principal:
        return 401, {"error": "unauthorized"}
    row = outbox.get(turn_id, principal=principal)
    if row is None:
        return 404, {"error": "turn_not_found"}
    return 200, row


def get_turn_audio(state: AppState, turn_id: str, *, auth=None) -> tuple[int, bytes]:
    outbox = getattr(state.app, "outbox", None)
    if outbox is None:
        return 503, b""
    principal = getattr(auth, "principal", "") or ""
    if not principal:
        return 401, b""
    audio = outbox.read_audio(turn_id, principal=principal)
    if not audio:
        return 404, b""
    return 200, audio


def prepare_send(state: AppState, body: dict, auth: AuthContext | None = None,
                 ip: str = "", source: str = "", external_id: str = "",
                 trusted_source: str = "") -> tuple[int, dict]:
    """校验并解析一次发送；成功时返回可直接展开给 `state.send` 的 kwargs。"""
    if auth is None:
        return _unauthorized()
    if not auth.can("chat"):
        return _forbidden()
    # 延時聚合（owner 2026-11-04）：App 前台把連續幾筆「文字／照片／表情包」攢成一輪送來。
    # 形狀換算交給 `server/api/items.py`：文字接行、表情包變 `[[stamp_in:…]]`、照片排成 media_list。
    items = body.get("items") if isinstance(body.get("items"), list) else []
    text = str(body.get("text") or "").strip()
    kind = str(body.get("kind") or "text").strip().lower()
    if items:
        text = ""                                  # 內容由 items 決定，下面再填
        kind = "text"
    elif not text and kind == "image":
        text = "（画像を送った）"          # 看圖那條：可以只送圖不帶字
    sticker_id = ""
    if not items and kind == "sticker":
        # 表情包（owner 2026-10-29）：她看不到圖，只看得到這張的「意思」（清單裡寫的那句）
        found = _sticker(state, body)
        if found is None:
            return 400, {"error": "unknown sticker"}
        sticker_id = found.id
        # ⚠️ **要寫清楚是「對方」發的**（2026-10-29 實測）。
        # 原本是 `（スタンプ：{意思}）` —— 沒有主體，模型讀到會以為**那是自己上一輪說的話**
        # ⇒ 它就按「接續自己的話」來回，**完全不叫 `sticker.send`**。
        # 加上主體（`相手が…`）之後，模型知道是主人發來的。
        # ⚠️ prompt 裡**不能用中文**（三位都是日語母語）⇒ 一律日文。
        # ⚠️ **用標籤格式，不要用日文句子的括號**（2026-10-29 實測）：
        # 原本寫成 `（相手がスタンプを送ってきた：…）` ⇒ 她**照抄那個格式**，
        # 在回覆正文裡寫出「（スタンプ：えへへ）」這種**假貼圖**（實測 7/12 輪）。
        # 改成 `[[stamp_in:…]]` 這種明顯不是「話」的標籤 ⇒ 她不會拿來當句子模板。
        # 這個標籤**只進 prompt**；客戶端顯示的是 `asked_sticker` 欄位（畫圖，不顯示字）。
        text = (f"[[stamp_in:{found.meaning}]]"
                + (f" {text}" if text else ""))
        kind = "text"
    if not text and not items:
        return 400, {"error": "text required"}
    if len(text) > 1000:
        return 400, {"error": "text too long"}
    if not state.app.auth.allow_chat(auth):
        return 429, {"error": "rate_limited", "retry_after_s": 60}

    entity, claimed = _id(body)
    principal = state.app.auth.resolve_principal(
        auth, claimed, source=source, external_id=external_id)
    if auth.role is Role.SERVICE:
        # 服務帳號（每個 bot 一把，2026-11-16）：只能替自己那顆 bot 的角色、已綁定的外部身分發回合
        if entity != str(auth.entity or ""):
            return 403, {"error": "entity_not_bound"}
        if not principal:
            return 403, {"error": "unknown_external_id"}
    # 聚合的那一輪：等 principal 有了才能驗照片的擁有者（media store 按 principal 隔離）
    media_list: list[dict] = []
    stickers_in: list[str] = []
    asked_parts: list[dict] | None = None
    asked_text = ""
    if items:
        from agent.server.api.items import build_items
        status, built = build_items(state, items, entity=entity, principal=principal)
        if status != 200:
            return status, built
        text = str(built["text"])
        if len(text) > 1000:                 # 多筆合起來也不能繞過單輪上限
            return 400, {"error": "text too long"}
        media_list = list(built["media_list"])
        stickers_in = list(built["stickers"])
        asked_parts = list(built["asked_parts"])
        asked_text = str(built["asked"])
        if media_list:
            kind = "image"                 # ⇒ `media.see` 管線（VisionStage 逐張看）
    # Guest 的 HTTP 入口只做一对一；群聊/无主/伪造房间由源适配器在认证后决定，
    # 不能让 guest 通过 body 把自己的话塞进别人的房间。
    # 模式（owner 2026-10-21 定）：
    #   · **主人默認就是 work**——他的話一律當工作指令執行；不傳 mode 也走 work。
    #     （主人若明說 chat，仍按 work 執行，只記一個 note：`owner_is_work`。）
    #   · **guest 一律 chat**——委任只有主人能開，guest 的 work 降級並記 `work_requires_owner`。
    requested = str(body.get("mode") or "").strip().lower()
    if requested and requested not in {"chat", "work"}:
        return 400, {"error": f"unknown mode: {requested}"}
    mode_note = ""
    if auth.is_owner:
        mode = "work"
        if requested == "chat":
            mode_note = "owner_is_work"
    else:
        mode = "chat"
        if requested == "work":
            mode_note = "work_requires_owner"
    if auth.is_owner:
        group = bool(body.get("group"))
        participants = _participants(body)
        ownerless = bool(body.get("ownerless"))
        mention = bool(body.get("mention"))
        addressed_to = str(body.get("addressed_to") or "").strip()
    else:
        group = False
        participants = None
        ownerless = False
        mention = False
        addressed_to = ""
    return 200, {
        "text": text, "entity": entity, "principal": principal,
        "group": group, "mention": mention, "addressed_to": addressed_to,
        "participants": participants, "ownerless": ownerless,
        "request_id": str(body.get("request_id") or ""),
        "admin": auth.is_owner,
        "mode": mode,
        "mode_note": mode_note,
        # 只有主人能要求「這一輪一定要出聲」（語音來源用；guest 不受理）
        "want_voice": bool(body.get("want_voice")) and auth.is_owner,
        # guest 的 body 不能自報渠道；音檔／圖片由 server 用 trusted_source 明確傳入。
        # MR 純文字沒有 body.source，也要沿 X-Source 分流，不能掉回 phone:owner。
        "source_override": (str(trusted_source or "").strip()
                            or (str(body.get("source") or "").strip() if auth.is_owner else "")
                            or (f"mr:{principal}" if end_of(source) == MR else "")),
        # 看圖那條（P4）：kind=image + media 一行 ⇒ 管線 media.see
        "kind": kind,
        "media": dict(body.get("media") or {}),
        "sticker": sticker_id,
        # 延時聚合（owner 2026-11-04）：一輪多張照片／多張表情／多行文字
        "media_list": media_list,
        "stickers": stickers_in,
        "asked_parts": asked_parts,
        "asked_text": asked_text,
    }


def _sticker(state: AppState, body: dict):
    catalog = getattr(state.app, "stickers", None)
    if catalog is None:
        return None
    entity = str(body.get("entity") or "mira").strip()
    return catalog.get(entity, str(body.get("sticker") or ""))


def list_stickers(state: AppState, query: dict, *, auth=None) -> tuple[int, dict]:
    """`GET /api/stickers?entity=`：這個角色那一包（兩端都能用；圖走 `/api/stickers/<entity>/<id>`）。"""
    if auth is None:
        return _unauthorized()
    if not auth.can("chat"):
        return _forbidden()
    entity = str((query.get("entity") or ["mira"])[0] or "mira").strip()
    catalog = getattr(state.app, "stickers", None)
    stickers = catalog.list(entity) if catalog is not None else []
    # 每張附內容雜湊：端末本機那份的雜湊一樣 ⇒ 那張連問都不用問（owner 2026-10-29 快取）
    rows = [dict(item.to_dict(), hash=catalog.hash_of(item)) for item in stickers]
    # `version`＝整包的雜湊（HTTP 層拿它當 ETag ⇒ 沒變就 304，整份清單不重傳）
    return 200, {"entity": entity, "stickers": rows, "version": json_tag(rows)}


def sticker_file(state: AppState, entity: str, sticker_id: str, *, auth=None):
    """`GET /api/stickers/<entity>/<id>` ⇒ `(status, path)`；找不到一律 404。"""
    if auth is None:
        return 401, None
    if not auth.can("chat"):
        return 403, None
    catalog = getattr(state.app, "stickers", None)
    found = catalog.get(entity, sticker_id) if catalog is not None else None
    path = catalog.path_of(found) if found is not None else None
    return (200, path) if path is not None else (404, None)


def prepare_audio(state: AppState, audio: bytes, *, headers: dict | None = None,
                  auth: AuthContext | None = None, ip: str = "",
                  source: str = "", external_id: str = "") -> tuple[int, dict]:
    """語音輸入：音檔 → 文字 → 走**同一道** `prepare_send`（權限/模式/長度全部照舊）。

    為什麼要合流：語音不該有自己的一套權限。轉出來的文字跟打字進來的一模一樣地校驗，
    所以 guest 用語音也拿不到委任，主人用語音也照樣默認 work。
    """
    headers = dict(headers or {})
    stt = getattr(state.app, "stt", None)
    if stt is None:
        return 503, {"error": "stt_unavailable"}
    if not audio:
        return 400, {"error": "audio required"}
    if len(audio) > 12_000_000:
        return 413, {"error": "audio too large"}
    language = str(headers.get("X-Lang") or "").strip().lower()
    # 語音訊息要不要「回語音」：`/api/audio` 的 body 是二進位音檔，沒地方放 JSON 欄位，
    # 所以用 header `X-Want-Voice`（owner 2026-10-22 要「語音回語音」）。
    # 權限照舊：prepare_send 那邊只讓 owner 的 want_voice 生效，guest 一律不出聲。
    want_voice = str(headers.get("X-Want-Voice") or "").strip().lower() in {"1", "true", "yes", "on"}
    received = time.monotonic()
    # 單人通話（owner 2026-10-29）：帶著 `X-Call` 的那一句是電話裡說的 ⇒ 來源標成 `phone:call`（MR 是 `mr:call`）
    # （實時源、可搶佔；prompt 會叫她講短）。只認主人、只認**這個角色現在那一通**——
    # 已經掛了／換了一通 ⇒ 410，App 收線，不把這句話當普通語音簡訊送出去。
    # （2026-10-30：挪到轉寫之前——掛掉的電話不必再花一次識別）
    call_id = str(headers.get("X-Call") or "").strip()
    # 哪一端送來的（`X-Source: mr`＝MR 頭顯；沒帶＝手機）：MR 的那一句標成 `mr:…`，
    # 手機的紀錄、推播都看不到它（各走各的管道，owner 2026-11-01；`domain/client_end.py`）
    end = end_of(source)
    source_tag = "mr:audio" if end == MR else "api:audio"
    ticket = None
    hold = getattr(state.app, "speech_hold", None)
    if call_id:
        if auth is None or not auth.is_owner:
            return 403, {"error": "forbidden"}
        entity_id = str(headers.get("X-Entity") or "").strip() or "mira"
        if state.app.voice.touch(entity_id, call_id) is None:
            # 被另一端接管 ⇒ `taken_over`（那一端要退出並明示）；其他 ⇒ `call_ended`（WO §7 W3）
            return 410, {"error": voice_api.ended_error(state, call_id)}
        source_tag = call_source(end)
        # 通話：先排號（後到的句子要等它），轉寫時順便判「說完了沒」
        if hold is not None:
            ticket = hold.begin(f"{entity_id}:{call_id}")
    suffix = ".wav" if audio[:4] == b"RIFF" else ".raw"
    if suffix == ".raw":
        audio = stt.pcm16_to_wav(audio, rate=int(headers.get("X-Rate") or 16000))
        suffix = ".wav"
    try:
        # 只有通話、且轉寫器裝了判斷器才多帶 `judge`（別的轉寫器實作不必認這個參數）
        extra = {"judge": True} if (ticket is not None
                                    and getattr(stt, "end_judge", None) is not None) else {}
        transcript = stt.transcribe(audio, language=language, suffix=suffix, **extra)
    except BaseException:
        if ticket is not None:
            hold.abandon(ticket)
        raise
    if not transcript.ok:
        if ticket is not None:
            hold.abandon(ticket)
        return 422, {"error": "stt_failed", "transcript": transcript.to_dict()}
    text = transcript.text
    held = None
    if ticket is not None:
        held = hold.resolve(ticket, transcript.text, transcript.complete)
        if held.superseded:
            # 這句沒講完、主人接著又講了 ⇒ 已經併進下一句，這裡不起回合
            return 200, {"merged": True, "transcript": transcript.to_dict(),
                         "input": "audio", "call": call_id}
        text = held.text
    input_timing = audio_timing(transcript, held, received)
    body = {"text": text,
            "entity": str(headers.get("X-Entity") or ""),
            "mode": str(headers.get("X-Mode") or ""),
            "request_id": str(headers.get("X-Request-Id") or ""),
            "want_voice": want_voice,
            # 語音輸入＝實時源（REALTIME）：並行、可搶佔、優先級最高
            "source": source_tag}
    status, data = prepare_send(state, body, auth=auth, ip=ip, source=source,
                                external_id=external_id, trusted_source=source_tag)
    if status != 200:
        return status, data
    data["input_timing"] = input_timing
    data["transcript"] = {**transcript.to_dict(), "text": text}
    data["input"] = "audio"
    data["call"] = call_id
    return status, data


def audio_timing(transcript, held, received: float) -> dict:
    """語音那一段的耗時（毫秒）：識別／糾錯／說完判斷／扣住等待，與「收到音檔→要起回合」總長。

    寫進 `turns.jsonl` 那一行的 `input`（`TurnService` 收），跟回合裡的 brain／首字／首音
    接成一條完整的時間線（2026-10-30；參考 airi `trace-kit` 的語音那條）。
    """
    def ms(seconds):
        return round(seconds * 1000) if seconds is not None else None

    return {
        "audio_s": getattr(transcript, "seconds", None),
        "asr_ms": ms(getattr(transcript, "asr_s", None)),
        "fix_ms": ms(getattr(transcript, "fix_s", None)),
        "judge_ms": ms(getattr(transcript, "judge_s", None)),
        "complete": getattr(transcript, "complete", None),
        "hold_ms": held.waited_ms if held is not None else None,
        "merged": held.merged if held is not None else 0,
        "ready_ms": ms(time.monotonic() - received),
    }


def post_send(state: AppState, body: dict, auth: AuthContext | None = None,
              ip: str = "", source: str = "",
              external_id: str = "") -> tuple[int, dict]:
    status, data = prepare_send(state, body, auth=auth, ip=ip,
                                source=source, external_id=external_id)
    if status != 200:
        return status, data
    try:
        return 200, state.send(**data)
    except KeyError as exc:
        return 400, {"error": f"unknown id: {exc}"}
    except Exception as exc:                       # noqa: BLE001 - Demo 要看得到錯誤
        traceback.print_exc()
        return 500, {"error": f"{type(exc).__name__}: {exc}"}


def post_location(state: AppState, body: dict, auth: AuthContext | None = None,
                  ip: str = "", source: str = "") -> tuple[int, dict]:
    """端末上報座標（`POST /api/location`）——天氣用。

    口徑沿用現役 BlueAgent：**位置按端末存**、app 是感測器、拿不到就讓天氣走 IP 概算。
    這裡只負責收與存，不做任何推斷。
    """
    if auth is None:
        return 401, {"error": "unauthorized"}
    try:
        lat = float(body.get("lat"))
        lon = float(body.get("lon"))
    except (TypeError, ValueError):
        return 400, {"error": "bad_coordinates"}
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return 400, {"error": "bad_coordinates"}
    device = str(body.get("device") or auth.principal or "unknown").strip() or "unknown"
    accuracy = body.get("accuracy")
    sensor = str(body.get("source") or "gps")
    if source == MR:
        sensor = "mr:" + sensor       # C-18 旧端点保留；与已迁往 App Server 的手机样本分域
    try:
        row = state.app.locations.save(
            device, lat, lon, principal=auth.principal,
            accuracy=None if accuracy is None else float(accuracy),
            source=sensor)
    except Exception as exc:                          # noqa: BLE001 - 存位置失敗不該炸 API
        return 500, {"error": f"save_failed:{type(exc).__name__}"}
    return 200, {"ok": True, "device": row["device"], "at": row["at"],
                 "principal": row["principal"], "ip": ip}
