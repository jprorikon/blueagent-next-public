"""回合執行：一次輸入 → 事件 → 跑管線並等它 → 決策／幀／封片／遙測／情緒。

review.md（2026-10-25）P1：這段原本是 `AppState.send()`（143 行），跟快照、訪客、串流、
語音後續全擠在同一個類。現在獨立成 `TurnService`，**只接它真的用到的物件**
（runtime、threads、pending、lives、principals，可選 telemetry／delivery／affect），
不接整個 App；所以可以不起 HTTP 層、不建 AppState 就單獨測。
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid

from agent.brain.compose.frames import FrameSink
from agent.brain.domain.event import IngressEvent, InputKind, Origin, SourceClass
from agent.brain.domain.pending import PendingIntervention

#: 語音源：這些前綴進來就是 REALTIME（並行、可搶佔、優先級最高）。
#: owner 2026-10-22：「為了保證實時性，語音來源會話優先度高」。
#: `phone:call`＝App 的單人通話（owner 2026-10-29）；`mr:`＝MR 頭顯那一端（`domain/client_end.py`）。
VOICE_SOURCES: tuple[str, ...] = ("mic:", "discord:vc", "voice:", "api:audio", "phone:mic",
                                 "phone:call", "mr:call", "mr:audio")

#: 環境源：這些前綴是背景（桌遊／提醒／時鐘），排隊且最低。
AMBIENT_SOURCES: tuple[str, ...] = ("trpg", "table", "clock:", "ambient")


def from_table(source: str) -> bool:
    """這一輪是**桌（TRPG）喊她**，不是主人在說話（`server/integrations/trpg.py` 以 principal=owner
    指名叫她出手）。2026-11-09 檢查：以前每一手都被記成「主人說話了／回了／在 App 上」——
    主動消息當成已回、心情當成主人陪她聊、主人缺席計時歸零、主動消息改走 App。"""
    return str(source or "").strip().lower().startswith(("trpg", "table"))


def source_class_for(source: str) -> SourceClass:
    raw = str(source or "").strip().lower()
    if raw.startswith(VOICE_SOURCES):
        return SourceClass.REALTIME
    if raw.startswith(AMBIENT_SOURCES):
        return SourceClass.AMBIENT
    return SourceClass.INTERACTIVE


def reply_text(result: dict) -> str:
    """從一輪的結果裡取回覆全文。

    ⚠️ 結果**沒有**頂層 `reply`/`text`：全文在 frames 的 `done` 幀裡
    （2026-10-22 踩到：後續交付一直送出空字串）。
    """
    for frame in reversed(result.get("frames") or []):
        if str(frame.get("kind")) == "done":
            return str((frame.get("payload") or {}).get("text") or "").strip()
    return str(result.get("reply") or result.get("text") or "").strip()


def reply_bubbles(result: dict) -> list[str]:
    """從一輪的結果裡取回覆**分段**（＝SSE 那些 `part` 幀的同一份切句，`reply_split`）。

    為什麼要有：串流時她是「一條一條短訊息」出現的，而 `/api/turns` 補拿／歷史
    只存全文 ⇒ 同一輪在補回來時變成一大條氣泡（owner 2026-11-04 指出的對齊問題）。
    落進 outbox 的 `parts` 就是那份分段，兩條路才長得一樣。

    ⚠️ 跟 `reply_text` 一樣：分段在 `done` 幀的 payload 裡，frames 被清掉之前就要取。
    """
    for frame in reversed(result.get("frames") or []):
        if str(frame.get("kind")) == "done":
            groups = (frame.get("payload") or {}).get("bubbles") or []
            return [str(item).strip() for item in groups if str(item).strip()]
    return []


def room_for(principals, principal: str, participants, group: bool,
             ownerless: bool) -> tuple[str, tuple[str, ...]]:
    """這一輪的發言人與在場名冊（群聊沒給名冊 ⇒ 所有已知的人）。"""
    if ownerless:
        principal = ""
    people = {str(who).strip() for who in (participants or ()) if str(who).strip()}
    if group or ownerless:
        if not people:
            people = {person.id for person in principals.all()}
        if not ownerless:
            people.add(principal)
        return principal, tuple(sorted(people))
    return principal, (str(principal),)


def routing_for(principal: str, group: bool, mention: bool, addressed_to: str,
                ownerless: bool, *, scope: str = "", scene: str = "") -> tuple[str, str, str, str]:
    """沒有標來源時的預設路由：`(source, scope, scene, addressed_to)`。

    `scope`／`scene` 由呼叫端指定時一律用它（REVIEW 2026-11-07 #1）：App 的群聊
    「3人の部屋」是 `group:<conversation_id>`／`app-group`，**不再**借用
    `group:discord-lobby`——否則 App 群聊與 Discord 大廳會寫進同一條 thread，
    兩邊的上下文互相污染。
    """
    if ownerless:
        return "trpg:table-1", "self:trpg-table-1", "trpg-table-1", "entity"
    source = f"discord:{principal}" if group else f"phone:{principal}"
    scope = str(scope or "").strip() or ("group:discord-lobby" if group else "")
    scene = str(scene or "").strip() or ("discord-lobby" if group else "together")
    target = "entity" if (not group or mention) else str(addressed_to or "").strip()
    return source, scope, scene, target


def job_wait_s(decision, *, mode: str = "chat") -> float:
    """等這一輪跑完的上限（秒）。

    ⚠ 原本預設 8 秒：她的回合經常 8.0–8.1 秒（實測），剛好卡在邊界上 ⇒ 提早返回、
    frames 只剩 memory+thinking、台詞 0 字（2026-10-22 取證）——是競態，不是模型。
    """
    wait = float(os.environ.get("BLUEAGENT_STREAM_WAIT_S") or 30.0)
    deadline = getattr(decision, "deadline_ms", None) if decision is not None else None
    if deadline:
        wait = max(wait, float(deadline) / 1000.0 + 4.0)
    # 工作模式常帶工具與 `delegate`（委任自己的 deadline 是 900 s）⇒ 30 秒一定不夠，
    # 到點就判「這一輪沒完成」，端末拿到空或半截、她後台還在跑（2026-11-02 實測 124 s 那筆）。
    if str(mode or "chat").strip().lower() == "work":
        wait = max(wait, float(os.environ.get("BLUEAGENT_STREAM_WAIT_WORK_S") or 150.0))
    return wait


class TurnService:
    def __init__(self, *, runtime, threads, pending_store, lives, principals,
                 telemetry=None, delivery=None, affect=None, on_decision=None,
                 pending_delivery=None, proactive=None, accounts=None) -> None:
        self.runtime = runtime
        self.threads = threads
        self.pending_store = pending_store
        self.lives = lives
        self.principals = principals
        #: 帳號表（`runtime/identity/accounts.py`）：平台帳號 id ↔ 大腦 principal 的翻譯（2026-11-09）
        self.accounts = accounts
        self.telemetry = telemetry
        self.delivery = delivery
        self.affect = affect
        #: 主人要她交付、她還沒交的東西（`runtime/inner/pending_delivery.py`）：這裡只記帳，
        #: 要不要交、怎麼交仍然是她自己的事（不擋回話、不改交付行為）。
        self.pending_delivery = pending_delivery
        #: 主動消息的閘：有人真的對她說話 ⇒ `note_heard`（她之前的主動消息算「回了」，可以再開口）
        self.proactive = proactive
        #: 每一輪的決策交給呼叫端（AppState 拿去放進 route_log）
        self.on_decision = on_decision
        self._telemetry_warned = False

    @classmethod
    def from_app(cls, app, *, on_decision=None) -> "TurnService":
        """從組好的 App 挑出需要的那幾樣（服務本身不持有整個 App）。"""
        return cls(runtime=app.runtime, threads=app.threads, pending_store=app.pending_store,
                   lives=app.lives, principals=app.principals,
                   telemetry=getattr(app, "telemetry", None),
                   delivery=getattr(app, "delivery", None),
                   affect=getattr(app, "affect", None), on_decision=on_decision,
                   accounts=getattr(app, "identity", None) or getattr(app, "accounts", None),
                   pending_delivery=getattr(app, "pending_delivery", None),
                   proactive=getattr(app, "proactive", None))

    # ── 一輪 ────────────────────────────────────────────────────────────────
    def run(self, text: str, *, entity: str = "mira", principal: str = "owner",
            group: bool = False, mention: bool = False, addressed_to: str = "",
            participants=None, ownerless: bool = False,
            request_id: str = "", replay: bool = False,
            force_reply: bool = False, want_voice: bool = False,
            mode: str = "chat", mode_note: str = "", room_note: str = "",
            quick: bool = False, followup: bool = False, ack_text: str = "",
            source_override: str = "", on_frame=None,
            scope_override: str = "", scene_override: str = "",
            kind: str = "text", media: dict | None = None, sticker: str = "",
            media_list: list | None = None, stickers: list | None = None,
            asked_parts: list | None = None, asked_text: str = "",
            input_timing: dict | None = None, on_job=None) -> dict:
        """跑一輪，回結果（不含應用快照——那是 AppState 的事）。

        `input_timing`：語音那一段（識別／糾錯／說完判斷／扣住）的耗時，原樣記進這一輪的 telemetry。
        `on_job`：回合一交給排程器就把 `Job` 交出去（主人指令佇列靠它在執行中取消：
        `job.cancel()` 只翻令牌，大腦在下一個邊界自己停，見 `owner_queue.CANCEL_HOOK`）。
        """
        text = str(text or "").strip()
        if not text:
            raise ValueError("text required")
        at = time.time()
        started = time.monotonic()           # 打點用的單調鐘（牆鐘會被 NTP 拉走）
        principal, room = room_for(self.principals, principal, participants, group, ownerless)
        source, scope, scene, target = routing_for(principal, group, mention,
                                                   addressed_to, ownerless,
                                                   scope=scope_override, scene=scene_override)
        if source_override:                     # 語音源（mic:/discord:vc/api:audio）由呼叫端標明
            source = str(source_override)
        yield_key = f"{entity}|{scope or principal}"
        if not replay:
            self.pending_store.cancel_key(yield_key)
        event = IngressEvent(
            principal=str(principal), entity=str(entity), source=source,
            kind=InputKind(str(kind or "text")), at=at, source_class=source_class_for(source),
            origin=Origin.SELF if ownerless else Origin.OWNED,
            reactions=group,
            scope=scope, scene=scene, addressed_to=target, participants=room,
            idempotency_key=request_id or f"req-{uuid.uuid4().hex}",
            payload={"text": text, "group": group, "participants": list(room),
                     "replay": replay, "force_reply": force_reply,
                     "want_voice": bool(want_voice),
                     "mode": str(mode or "chat"), "mode_note": str(mode_note or ""),
                     # App 房裡「上次看過之後」的那幾則＋讀前文的工具提示（adapter 從 App 取的，見 [[room]]）
                     "room_note": str(room_note or ""),
                     "quick": bool(quick), "ack": bool(quick),
                     # 語音「先短回一句」那條：followup=這一輪是補完的正式回答，
                     # ack_text=剛剛已經說過的那句（prompt 靠它避免開頭重複，owner 2026-10-22）
                     "followup": bool(followup), "ack_text": str(ack_text or ""),
                     # 看圖那條（P4）：`media` 是 MediaStore 的一行（id/ext/bytes/path）
                     "media": dict(media or {}),
                     # 延時聚合（owner 2026-11-04）：一輪多張圖，VisionStage 逐張看
                     "media_list": [dict(item) for item in (media_list or [])],
                     # 他這一輪送來的表情（可能不只一張；文字那邊已經換成 [[stamp_in:…]]）
                     "stickers_in": [str(item) for item in (stickers or [])],
                     # 給客戶端歷史畫的「他那幾條」（kind/text/sticker/media_id）
                     "asked_parts": [dict(item) for item in (asked_parts or [])],
                     "asked_text": str(asked_text or ""),
                     # 主人送來的表情（id；文字那邊已經換成「（相手がスタンプを送ってきた：意思）」）
                     "sticker": str(sticker or "")},
        )
        sink = FrameSink(on_frame=on_frame)
        # 主人要她交付（「送って」「見せて」「チェックするから」…）⇒ 記一筆 unfulfilled；
        # 她這一輪真的送出 photo／artifact ⇒ 清掉（2026-11-08）。
        if not from_table(source):        # 桌上的 prompt 不是主人的請求
            self._note_delivery_request(entity, principal, text, at=at, source=source, replay=replay)
        result, finished = self._execute(event, sink, entity=entity,
                                         human=not ownerless and not replay,
                                         mode=str(mode or "chat"), on_job=on_job)

        decision = result.decision.to_dict() if result.decision is not None else None
        if decision is not None:
            decision["frames"] = sink.snapshot()
            decision["engine"] = "blueagent-next"
            if self.on_decision is not None:
                self.on_decision(decision)
        error = ""
        if result.job is not None and result.job.error is not None:
            error = f"{type(result.job.error).__name__}: {result.job.error}"
        elif not finished:
            # 等待截止不代表回合成功；後台仍可能完成，但這個响应不是成功终态。
            error = "TimeoutError: turn execution is still pending"
        # App（`app:` 來源）的回覆只由 AppChatAdapter 送回 App Server；重放走的是 state.send，
        # 沒有人會把那句送進 App 房間 ⇒ 不排重放，這一輪的 YIELD 就是合法的沉默
        # （App 群聊本來就「獲選者沉默也不讓別人補位」）。REVIEW 2026-11-08。
        if result.yielded and not str(source_override or "").startswith("app:"):
            self.pending_store.add(PendingIntervention.create(
                key=yield_key, text=text, entity=entity, principal=principal,
                group=group, mention=mention, addressed_to=addressed_to,
                participants=room, yield_ms=result.yield_ms or 8000,
                source_override=source_override, scope_override=scope_override,
                scene_override=scene_override))
        sealed = None
        if result.sealed is not None:
            sealed = {"id": result.sealed.id, "reason": result.sealed.reason.value,
                      "turns": len(result.sealed.turns),
                      "summary": result.sealed.summary}
        frames = sink.snapshot()
        # 她真的送出去了 ⇒ 這筆欠帳結清（photo＝圖已進媒體倉、Discord/App 兩端都拿得到）
        self._clear_delivered(entity, principal, frames, replay=replay)
        if not replay:
            self._record_telemetry(result, decision, frames, sink, started=started, at=at,
                                   entity=entity, principal=principal, mode=mode,
                                   chars=len(text), error=error, input_timing=input_timing)
        # 只有「真的在對話」才算；旁聽（observed）不算——旁聽是別人的事，不該左右她的心情。
        # 桌喊她出手（`from_table`）也不算：那不是主人在說話。
        if not from_table(source):
            self._note_channel(principal, source_override=source_override, group=group)
        if not bool(result.observed) and not quick and not from_table(source):
            self._note_affect(entity, text, principal, source=source)
            self._note_heard(entity, principal, at)
        return {
            "ok": not error, "error": error, "finished": finished,
            "observed": result.observed, "dropped": result.dropped,
            "duplicate": result.duplicate, "reacted": result.reacted,
            "yielded": result.yielded,
            "yield_ms": result.yield_ms,
            # 這一輪從哪個端進來（`phone:owner`／`discord:owner`…）：outbox 要按它分流，
            # App 的對話紀錄只認 `phone:`（owner 2026-10-27）。
            "source": str(source),
            "mode": str(mode or "chat"), "mode_note": str(mode_note or ""),
            "decision": decision, "frames": frames, "sealed": sealed,
            "room": list(room),
        }

    def _execute(self, event, sink, *, entity: str, human: bool, mode: str = "chat", on_job=None):
        """交給 runtime 並等它跑完；人在說話時暫停**這個角色**自己的生活。"""
        # 只暫停「這個角色」自己的生活：主人跟ミラ說話時，其他人跟ルミ說話不受影響
        life = self.lives.get(str(entity))
        if human and life is not None:
            life.human_enter()
        try:
            result = self.runtime.handle(event, source=sink)
            finished = True
            if result.job is not None and on_job is not None:
                try:
                    on_job(result.job)
                except Exception:                      # noqa: BLE001 - 登記失敗只是不能中途取消
                    pass
            if result.job is not None:
                finished = result.job.wait(job_wait_s(result.decision, mode=mode))
        finally:
            if human and life is not None:
                life.human_exit()
            self.threads.persist()
        return result, finished

    # ── 每一輪的旁帳（任何一項壞掉都不能影響對話）──────────────────────────────
    def _record_telemetry(self, result, decision, frames, sink, *, started: float, at: float,
                          entity: str, principal: str, mode: str, chars: int,
                          error: str, input_timing: dict | None = None) -> None:
        """一輪一行：結構化紀錄（看板/排障用；不記原文、不記 token）。"""
        if self.telemetry is None:
            return
        try:
            self.telemetry.record(
                thread=str(getattr(result.decision, "thread_id", "") or ""),
                entity=str(entity), principal=str(principal),
                engage=str(getattr(result.decision, "engage", "")),
                mode=str(mode or "chat"), pipeline=str(decision.get("pipeline")
                                                        if decision else ""),
                ms=round((time.time() - at) * 1000),
                # 分段打點（2026-10-24）：全部相對這一輪開始：brain＝LLM 開跑、
                # first_token＝LLM 首字、first_audio＝首句音檔就緒。
                **sink.timing(started),
                # 工具分層的量測：一開始給了幾支、多少字、這一輪解鎖了哪些（`runtime/tools/channels.py`）
                **(sink.notes() if hasattr(sink, "notes") else {}),
                frames=len(frames),
                speech=sum(1 for frame in frames if frame.get("kind") == "speech"),
                speech_errors=sum(1 for frame in frames if frame.get("kind") == "speech_error"),
                # 為什麼沒聲音：把原因留下來（`tts_down` / `tts_failed: …`）
                speech_error_reasons=[str((frame.get("payload") or {}).get("reason") or "")[:120]
                                      for frame in frames
                                      if frame.get("kind") == "speech_error"][:4],
                observed=bool(result.observed), dropped=bool(result.dropped),
                chars=chars, error=error,
                # 語音輸入那一段（收到音檔 → 起回合之前）；打字的回合沒有這欄
                **({"input": dict(input_timing)} if input_timing else {}),
            )
        except Exception as exc:                   # noqa: BLE001 - 紀錄不能影響對話
            if not self._telemetry_warned:
                self._telemetry_warned = True
                print(f"[telemetry] 紀錄失敗（{type(exc).__name__}: {exc}）", flush=True)

    def _note_channel(self, principal: str, *, source_override: str, group: bool) -> None:
        """「他剛剛在哪裡說話」：主動發言優先回到同一個信道（見 runtime/delivery/delivery.py）。"""
        if self.delivery is None:
            return
        channel = "discord" if str(source_override or "").startswith("discord") \
            else ("discord" if group else "app")
        try:
            self.delivery.note_seen(str(principal), channel)
        except Exception:                          # noqa: BLE001
            pass

    def _note_delivery_request(self, entity: str, principal: str, text: str, *,
                               at: float, source: str = "", replay: bool = False) -> None:
        """主人要她交付 ⇒ 記一筆 unfulfilled（只記帳，不改這一輪的行為）。

        判據在規則層（`pending_delivery.looks_like_delivery_request`）：像就記、不像就不記，
        原文照存。**客人不算**：只有 `Principal.owner` 的請求才進欠帳。
        """
        if replay or self.pending_delivery is None:
            return
        try:
            who = self.principals.get(str(principal)) if self.principals is not None else None
            if who is None or not getattr(who, "owner", False):
                return
            self.pending_delivery.note_request(str(entity), str(principal), text,
                                               at=at, source=source)
        except Exception as exc:                  # noqa: BLE001 - 記帳壞了不能擋回話
            print(f"[delivery] 記帳略過（{type(exc).__name__}: {exc}）", flush=True)

    def _clear_delivered(self, entity: str, principal: str, frames, *,
                         replay: bool = False) -> None:
        """這一輪真的送出東西（`photo`／`artifact` 幀）⇒ 結清欠帳。

        只用「有沒有交付幀」判定，不看文字怎麼說：她嘴上說「送った」不算，檔案出去才算。
        """
        if replay or self.pending_delivery is None:
            return
        kinds = {str(frame.get("kind") or "") for frame in (frames or []) if isinstance(frame, dict)}
        if not kinds & {"photo", "artifact", "file"}:
            return
        try:
            self.pending_delivery.clear_delivered(str(entity), principal=str(principal))
        except Exception as exc:                  # noqa: BLE001
            print(f"[delivery] 結清略過（{type(exc).__name__}: {exc}）", flush=True)

    def _note_heard(self, entity: str, principal: str, at) -> None:
        """common-ground 第 4 步：她先前的主動消息「回了」（不疊著發那條的解鎖信號）。

        平台那條進來的 principal 是**帳號 id**（`acct_owner`），主動消息的閘用**大腦 principal**（`owner`）
        當鍵；不翻譯就會「他明明回了，她還以為沒回」（2026-11-09 實測：真實 turns 裡兩種都在）。
        """
        if self.proactive is None or not hasattr(self.proactive, "note_heard"):
            return
        who = str(principal)
        if self.accounts is not None:
            try:
                who = str(self.accounts.principal_of(who) or who)
            except Exception:                     # noqa: BLE001 - 翻不了就用原值
                who = str(principal)
        try:
            self.proactive.note_heard(str(entity), who,
                                      at=float(at) if at else None)
        except Exception as exc:                  # noqa: BLE001 - 記不上不能擋回話
            print(f"[proactive] 記「回了」略過（{type(exc).__name__}: {exc}）", flush=True)

    def _note_affect(self, entity: str, text: str, principal: str, source: str = "") -> None:
        """有人對她說話 ⇒ 主人：message＋判定；客人／同伴：一個小 outcome（不算「主人說話」）。

        `source` 一起帶進去：**30 秒內、非同源、同一句話**由 `note_message` 併成一件
        （owner 2026-11-01）。

        owner 2026-11-09（AEM 稽核第 1 組）：以前**任何** principal 都記成 message ＋ 固定
        interest(+0.18/+0.28)、target 寫 owner——客人的話、別的角色在群組裡的話都推同一條心情，
        還重置「主人多久沒來」（缺席計時）⇒ 群組一熱鬧她就永遠不會想主人。實測：今天 nori 的
        20 條 inbound 裡 8 條不是主人（guest-x 7、ミラ 1），aster 2 條（ミラ／acct_owner）。
        """
        if self.affect is None:
            return
        try:
            who = str(principal)
            if self.accounts is not None:
                try:
                    who = str(self.accounts.principal_of(who) or who)
                except Exception:                 # noqa: BLE001 - 翻不了就用原值
                    who = str(principal)
            person = self.principals.get(who) if self.principals is not None else None
            if not getattr(person, "owner", False):
                # 客人／同伴：有一點情緒，但不寫 message（不動 last_user_message_at／缺席）
                self.affect.outcome(str(entity), kind="interest", delta_v=0.05, delta_a=0.10,
                                    confidence=0.5, event_id=f"talk:{who}:{source}:"
                                    + hashlib.sha1(str(text).encode()).hexdigest()[:12],
                                    target_type="conversation", target_id=who)
                return
            noted = self.affect.note_message(str(entity), text, speaker=who, source=source)
            if noted.get("ok"):
                # dampen：同種同幅連續 3 次後折半（情感整理 P1：以前只接在群聊的 SPIN，私聊每句都滿額）
                self.affect.appraise(str(entity), kind="interest", delta_v=0.18,
                                     delta_a=0.28, confidence=0.6, dampen=True,
                                     source_event_id=str(noted["event_id"]),
                                     target_type="owner", target_id=who)
        except Exception as exc:                  # noqa: BLE001 - 情緒層不能擋對話
            print(f"[affect] 判定略過（{type(exc).__name__}: {exc}）", flush=True)


__all__ = ["AMBIENT_SOURCES", "TurnService", "VOICE_SOURCES", "from_table", "job_wait_s", "reply_text",
           "room_for", "routing_for", "source_class_for"]
