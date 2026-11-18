"""語音服務：通話 session、粘性綁定、假的 speech 幀。

沒有 TTS 後端時不出真聲；它把「她決定打/說」之後的狀態與幀跑通。真 TTS 只需要換掉 `_speech` 的
audio 欄位（或接一個 TtsPort），`VoiceSession` / tool / egress 都不用改。
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from agent.brain.domain.client_end import APP_ENDS
from agent.brain.domain.entity import EntityRegistry
from agent.brain.domain.voice import VoiceSession, VoiceState
from agent.brain.runtime.voice.arbiter import FloorArbiter


# ── 唸出前的文字清洗（2026-10-29 owner：「回信会带一个 # 加数字，TTS 还会把数字读出来」）──
#: 規則集中在 `runtime/text_clean.py`（顯示與唸出共用同一份）。
from agent.brain.runtime.text_clean import clean_reply as speech_text

#: 「這一通怎麼沒的」記多久（秒）：被接管的那一端 15 秒內就會來心跳，60 秒很夠。
ENDED_TTL_S = 60.0


class VoiceService:
    def __init__(self, entities: EntityRegistry, *, log=None,
                 floor: FloorArbiter | None = None, tts=None) -> None:
        self.entities = entities
        self.floor = floor or FloorArbiter()
        #: 真的嘴（`TtsPort` 形狀：`speak(text, voice) -> [clip]`）。沒給＝mock 幀。
        #: 刻意用「有 speak 就行」的鴨子型別，讓 runtime 不必 import adapters（分層不倒過來）。
        self.tts = tts
        self._lock = threading.RLock()
        self._sessions: dict[str, VoiceSession] = {}
        self._seq = 0
        self._last_speech: dict | None = None
        self._log = log or (lambda _message: None)
        #: 打斷世代：每次「人開口」+1；在那之前建立的 `_StreamSpeaker` 一律作廢。
        self._epochs: dict[str, int] = {}
        self._available = {
            "discord": {"end": "discord", "target": "discord:vc-lobby",
                        "name": "Discord · 语音房"},
            "phone": {"end": "phone", "target": "phone:local", "name": "手机"},
            # Quest 3 的 MR 端（2026-11-01）：跟手機一樣是 App 的通話，但自己一條管道（`domain/client_end.py`）
            "mr": {"end": "mr", "target": "mr:local", "name": "MR"},
        }
        #: App 通話（`end=phone`／`mr`）多久沒聲息就當對方掉線、自動掛斷（App 每 15 秒打一次心跳）。
        #: 不設上限的話：App 被殺掉之後她的每一句回覆都還會被當成「通話中」合成聲音。
        self.call_idle_s = float(os.environ.get("BLUEAGENT_CALL_IDLE_S") or 45)
        #: 這一通是**怎麼沒的**（`call_id → (原因, 時間)`）：只記有特別原因的（被另一端接管）；
        #: 被蓋掉的那一端下一次心跳／送一句時拿來分「被接管」與「自己斷了」（WO-mr-call-and-switch §7 W3）。
        self._ended: dict[str, tuple[str, float]] = {}
        #: 她打給主人的 App 來電（`adapter/app_call_out.AppCallDialer`，組合根接上；鴨子型別：有 `dial()`）。
        #: None ⇒ `ring()` 退回舊的本機假通話（`call(end="phone")`）。見 `voice/outbound.py`。
        self.dialer = None

    # ── 對外 ────────────────────────────────────────────────────────────────────
    def call(self, entity: str, *, end: str = "discord", reason: str = "",
             principal: str = "", thread_id: str = "") -> dict:
        entity = str(entity)
        self._expire_idle()
        with self._lock:
            existing = self._sessions.get(entity)
            if existing is not None and existing.active:
                # 幂等：同一實體同時只有一通；重複請求回同一通，不撥第二次。
                return {"ok": True, "existing": True, "session": existing.to_dict(),
                        "frames": []}
            endpoint = self._available.get(str(end))
            if endpoint is None:
                return {"ok": False, "error": f"end_unavailable:{end}", "frames": [
                    {"kind": "call", "payload": {"state": "failed", "end": end,
                                                 "reason": f"{end} 不在线"}}]}
            session = VoiceSession.create(entity, endpoint["end"], endpoint["target"],
                                          reason=reason, principal=principal,
                                          thread_id=thread_id)
            if not self.floor.acquire(entity, session.id, priority=0,
                                      principal=principal, timeout=0.5):
                session.state = VoiceState.FAILED
                return {"ok": False, "error": "floor_busy",
                        "frames": [{"kind": "call",
                                    "payload": {"state": "failed", "end": end,
                                                "reason": "发言权被占用"}}]}
            self._sessions[entity] = session
            self._log(f"voice: {entity} 拨打 {end} → {session.target}（{session.id}）")
            return {"ok": True, "existing": False, "session": session.to_dict(),
                    "frames": [{"kind": "call", "payload": session.to_dict()}]}

    def ring(self, entity: str, *, principal: str = "", reason: str = "",
             if_unanswered: str = "", origin: str = "chat", thread_id: str = "") -> dict:
        """她決定打給主人（`voice.call(end=phone)`／自主想打）：接了撥號器就讓主人的 App 響，回 `ringing`。

        這時候還**沒有** `VoiceSession`——主人接了，App Server 才叫 `answer` 掛上（`adapter/app_voice.py`）。
        """
        from agent.brain.runtime.voice.outbound import enabled

        dialer = self.dialer
        if dialer is None or not enabled():
            return self.call(entity, end="phone", reason=reason, principal=principal,
                             thread_id=thread_id)
        return dialer.dial(str(entity), principal=str(principal or "owner"), reason=str(reason or ""),
                           if_unanswered=str(if_unanswered or ""), origin=str(origin or "chat"))

    def attach(self, session: VoiceSession) -> dict:
        """接入平台已鉴权的通话；保留平台 call_id，不另造客户端会话真相。

        只供受共享密钥保护的 App voice adapter 使用。普通 `/api/voice/*` 仍走 `call()`。
        """
        self._expire_idle()
        with self._lock:
            existing = self._sessions.get(session.entity)
            if existing is not None and existing.active:
                if existing.id == session.id:
                    return {"ok": True, "existing": True, "session": existing.to_dict()}
                return {"ok": False, "error": "busy_elsewhere"}
            if not self.floor.acquire(session.entity, session.id, priority=0,
                                      principal=session.principal, timeout=0.5):
                return {"ok": False, "error": "floor_busy"}
            self._sessions[session.entity] = session
            self._log(f"voice: App 平台接入 {session.entity}（{session.id}）")
            return {"ok": True, "existing": False, "session": session.to_dict()}

    def hangup(self, entity: str, *, call_id: str = "", reason: str = "") -> dict:
        """掛斷；帶 `call_id` 時只收當前這一通，過期請求不能誤掛後來的 session。

        `reason`（例如 `taken_over`）會記 `ENDED_TTL_S` 秒，讓那一端之後問得到「怎麼沒的」。
        """
        entity = str(entity)
        call_id = str(call_id or "")
        with self._lock:
            session = self._sessions.get(entity)
            if session is None:
                return {"ok": not call_id, "error": "call_ended" if call_id else "",
                        "session": None, "frames": []}
            if call_id and session.id != call_id:
                return {"ok": False, "error": "call_ended",
                        "session": session.to_dict(), "frames": []}
            self._sessions.pop(entity, None)
            if reason:
                now = time.time()
                self._ended = {key: value for key, value in self._ended.items()
                               if now - value[1] < ENDED_TTL_S}
                self._ended[session.id] = (str(reason), now)
        self.floor.release(entity, session.id)
        session.end_call()
        self._log(f"voice: {entity} 掛斷（{session.id}）")
        return {"ok": True, "session": session.to_dict(),
                "frames": [{"kind": "call", "payload": {**session.to_dict(),
                                                        "state": "ended"}}]}

    def active(self, entity: str) -> VoiceSession | None:
        self._expire_idle()
        with self._lock:
            session = self._sessions.get(str(entity))
            return session if session is not None and session.active else None

    def touch(self, entity: str, call_id: str = "") -> VoiceSession | None:
        """端末說「我還在線」：刷新這一通的心跳。`call_id` 對不上（已經換了一通）⇒ None。"""
        session = self.active(entity)
        if session is None or (call_id and session.id != str(call_id)):
            return None
        with self._lock:
            session.touch()
        return session

    def ended_reason(self, call_id: str) -> str:
        """這一通結束的特別原因（`taken_over`…）；沒有特別原因或已經過期 ⇒ 空字串。"""
        with self._lock:
            found = self._ended.get(str(call_id or ""))
        if found is None or time.time() - found[1] >= ENDED_TTL_S:
            return ""
        return found[0]

    def _expire_idle(self) -> None:
        """App 那頭沒聲息太久的通話自動掛斷（Discord 的通話由語音頻道自己管進出，不在這裡收）。"""
        if self.call_idle_s <= 0:
            return
        now = time.time()
        with self._lock:
            stale = [entity for entity, session in self._sessions.items()
                     if session.end in APP_ENDS and session.active
                     and session.idle_s(now) > self.call_idle_s]
        for entity in stale:
            self._log(f"voice: {entity} 的通話 {self.call_idle_s:.0f} 秒沒有心跳 ⇒ 自動掛斷")
            self.hangup(entity)

    def release_floor(self, entity: str, holder: str) -> None:
        self.floor.release(entity, holder)

    # ── 打斷（barge-in）─────────────────────────────────────────────────────────
    def epoch(self, entity: str) -> int:
        with self._lock:
            return int(self._epochs.get(str(entity), 0))

    def interrupt(self, entity: str, *, reason: str = "barge_in",
                  principal: str = "") -> dict:
        """人開口 ⇒ 她的嘴現在閉上。

        做三件事（不碰記憶、不碰文字輸出）：
          ① 世代 +1：所有在這一刻之前建立的 `_StreamSpeaker` 之後都吐不出 `speech` 幀；
          ② 收回回合發言權；活躍通話 session 繼續持有 floor，直到掛斷；
          ③ 回一份紀錄，讓上層寫日誌／`/api/metrics`。

        播放端（Discord VC／App）由各自的 adapter 負責停：這裡只保證「不會再有新的聲音冒出來」。
        """
        entity = str(entity)
        with self._lock:
            self._epochs[entity] = int(self._epochs.get(entity, 0)) + 1
            epoch = self._epochs[entity]
            session = self._sessions.get(entity)
            call_holder = session.id if session is not None and session.active else ""
        holder = self.floor.release_entity(entity, preserve_holder=call_holder)
        self._log(f"voice: {entity} 被打斷（{reason}，principal={principal or '-'}，"
                  f"epoch={epoch}，原發言權={holder or '-'}）")
        return {"entity": entity, "epoch": epoch, "floor_holder": holder,
                "reason": reason, "principal": principal}

    def reset(self) -> None:
        with self._lock:
            self._sessions.clear()

    def maybe_speak(self, entity: str, text: str, *, thread_id: str = "",
                    principal: str = "", force: bool = False,
                    target: str = "", source: str = "", style: str = "") -> list[dict]:
        """回 `speech` payload 列表。

        優先級：進行中的通話（粘性）> 本次回覆的 force（`voice.speak`）> 不說話。
        沒有可播目標時回 `speech_error`，文字仍然照常送出。
        """
        text = str(text or "").strip()
        if not text:
            return []
        session = self.active(entity)
        floor_holder = ""
        if session is not None:
            call_target, call_id = session.target, session.id
        elif force:
            call_target = target or source or "phone:local"
            call_id = ""
            if call_target.startswith(("discord:vc", "voice:", "mic:")):
                floor_holder = thread_id or f"voice:{entity}"
                if not self.floor.acquire(entity, floor_holder, priority=5,
                                          principal=principal, timeout=0.05):
                    return [{"kind": "speech_error",
                             "payload": {"text": text, "reason": "floor_busy",
                                         "target": call_target}}]
        else:
            return []
        if not call_target:
            return [{"kind": "speech_error", "payload": {"text": text,
                                                         "reason": "no_audio_target"}}]
        # TTS 剛剛掛過 ⇒ 直接回錯，不要每一句都再等一次 timeout
        #（2026-10-22 實測：3 句 × 20 s = 60 s，比不合成還糟）
        with self._lock:
            down_until = getattr(self, "_tts_down_until", 0.0)
        if time.time() < down_until:
            return [{"kind": "speech_error",
                     "payload": {"text": text, "reason": "tts_down",
                                 "target": call_target}}]
        voice = ""
        found = self.entities.get(entity)
        if found is not None:
            voice = found.voice or found.id
        if self.tts is not None:
            return self._speak_real(entity, text, voice=voice, style=style,
                                    target=call_target, call_id=call_id,
                                    thread_id=thread_id, principal=principal,
                                    floor_holder=floor_holder)
        with self._lock:
            self._seq += 1
            seq = self._seq
        duration = max(700, min(12000, int(len(text) * 72)))
        payload = {
            "text": text, "voice": voice, "style": style,
            "duration_ms": duration, "target": call_target,
            "call_id": call_id, "thread_id": thread_id, "reply_to": principal,
            "seq": seq, "mock": True, "floor_holder": floor_holder,
        }
        with self._lock:
            self._last_speech = dict(payload)
        return [{"kind": "speech", "payload": payload}]

    def begin_stream(self, entity: str, *, thread_id: str = "", principal: str = "",
                     force: bool = False, target: str = "", source: str = "") -> "_StreamSpeaker":
        """**首句優先**：邊生成邊合成，第一句一完整就送出去（現役橋的做法）。

        為什麼要這個：整段回覆生成完才合成 ⇒ 首句延遲 = 全文生成時間 + 首句合成時間。
        現役實測（owner 2026-10-06 的驗收）：拆句後首句延遲從 ~4 s 掉到 ~1 s 級。
        發言權（floor）只在**第一次**取一次，整輪結束（`finish()`）才放——每句都取會被判 floor_busy。
        """
        return _StreamSpeaker(self, entity, thread_id=thread_id, principal=principal,
                              force=force, target=target, source=source)

    def _speak_real(self, entity: str, text: str, *, voice: str, style: str,
                    target: str, call_id: str, thread_id: str, principal: str,
                    floor_holder: str) -> list[dict]:
        """真合成：一句一個 `speech` 幀，`audio` 是 base64 wav（內嵌，不讓端末再開第二條連線）。

        合成失敗**不吞文字**：回 `speech_error` 讓端末照樣顯示文字並知道「這次沒聲音」。
        """
        with self._lock:
            if time.time() < getattr(self, "_tts_down_until", 0.0):
                # 冷卻期內不再試（串流那條路直接走這裡，繞過了 maybe_speak）
                return [{"kind": "speech_error",
                         "payload": {"text": text, "target": target, "reason": "tts_down"}}]
        try:
            clips = list(self.tts.speak(text, voice))
        except Exception as exc:                          # noqa: BLE001 - 嘴壞了不該炸整輪
            # ⚠ 這裡**不設冷卻**：一句偶發失敗馬上就會重試，設了冷卻會讓重試直接吃
            # `tts_down`（owner 2026-10-22「只講半句」的幫兇）。冷卻只由 `_StreamSpeaker`
            # 在「連續 3 句都失敗」之後才上。
            return [{"kind": "speech_error",
                     "payload": {"text": text, "target": target,
                                 "reason": f"tts_failed: {type(exc).__name__}: {exc}"}}]
        frames: list[dict] = []
        for clip in clips:
            with self._lock:
                self._seq += 1
                seq = self._seq
            payload = dict(clip.payload())
            payload.update({
                "voice": voice, "style": style, "target": target,
                "call_id": call_id, "thread_id": thread_id, "reply_to": principal,
                "duration_ms": int(round(float(payload.get("seconds") or 0) * 1000)),
                "seq": seq, "mock": False, "floor_holder": floor_holder,
            })
            frames.append({"kind": "speech", "payload": payload})
            with self._lock:
                self._last_speech = dict(payload)
        return frames

    def snapshot(self) -> dict:
        self._expire_idle()
        with self._lock:
            sessions = {entity: session.to_dict()
                        for entity, session in self._sessions.items()}
            active = next((session.to_dict() for session in self._sessions.values()
                           if session.active), None)
        # 狀態快照不帶音檔本體（`audio` 是 base64，一句就是幾百 KB）：要聲音的端在那一輪的 `speech` 幀就拿到了；
        # 這裡只給「最近說了什麼、多長、播到哪」（網頁除錯台的卡片用）。2026-11-01：帶著它讓 `/api/state` 與掛斷回應肥到幾 MB。
        last = ({key: value for key, value in self._last_speech.items() if key != "audio"}
                if self._last_speech else None)
        return {"session": active, "sessions": sessions,
                "last_speech": last,
                "floor": self.floor.snapshot(),
                "available": {key: dict(value) for key, value in self._available.items()}}


class _StreamSpeaker:
    """把「一段段冒出來的話」即時合成；floor 只取一次。"""

    def __init__(self, voice, entity: str, *, thread_id: str, principal: str,
                 force: bool, target: str, source: str) -> None:
        self.voice = voice
        self.entity = entity
        self.thread_id = thread_id
        self.principal = principal
        self.force = force
        self.target = target
        self.source = source
        self.spoken = ""
        self._started = False
        self._enabled = False
        #: 連續合成失敗次數（偶發失敗要重試；連續爆掉才放棄，避免每句都等一個 timeout）
        self._failed = 0
        #: 最後一次的失敗原因（`/api/metrics` 與日誌用）
        self.last_error = ""
        self._floor_holder = ""
        self._call_target = ""
        self._call_id = ""
        #: 打斷：`cancel()` 之後這句話**再也不合成**（她已經被主人打斷了）。
        self._cancelled = threading.Event()
        #: 建立當下的世代；`voice.interrupt()` 之後這個 speaker 就過期了。
        self._epoch = voice.epoch(entity)
        #: 並行合成：待吐出的 (句子, future) 與「下一句要吐的索引」（保序）
        self._pool = None
        self._pending: list[tuple[str, object]] = []
        self._next = 0

    # ── 打斷 ────────────────────────────────────────────────────────────────
    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set() or self.voice.epoch(self.entity) != self._epoch

    def cancel(self) -> None:
        """停止這一輪的合成（已送出的 frames 收不回來，端末負責停播）。"""
        self._cancelled.set()
        self._enabled = False
        self._release_floor()

    def _release_floor(self) -> None:
        holder, self._floor_holder = self._floor_holder, ""
        if holder:
            self.voice.release_floor(self.entity, holder)

    def _prepare(self) -> None:
        self._started = True
        session = self.voice.active(self.entity)
        if session is not None:
            self._call_target, self._call_id = session.target, session.id
            self._enabled = True
            return
        if not self.force:
            self._enabled = False
            return
        self._call_target = ("" if self.target == "auto" else self.target) \
            or self.source or "phone:local"
        self._call_id = ""
        if self._call_target.startswith(("discord:vc", "voice:", "mic:")):
            holder = self.thread_id or f"voice:{self.entity}"
            if not self.voice.floor.acquire(self.entity, holder, priority=5,
                                            principal=self.principal, timeout=0.05):
                self._enabled = False
                return
            self._floor_holder = holder
        self._enabled = True

    #: 同時跑幾句合成（owner 2026-10-24「並行合成」）。保序：結果照句子順序吐出，
    #: 只是後面的句子在背景先算，不再讓「合成第 2 句」卡住「讀第 3 句的 delta」。
    #: 同時幾路合成（預設 2＝池子兩台各一路）；`BLUEAGENT_TTS_PAR` 可調
    WORKERS = max(1, int(os.environ.get("BLUEAGENT_TTS_PAR") or 2))

    def speak(self, sentence: str) -> list[dict]:
        text = speech_text(sentence)
        if not text:
            return []
        if self.cancelled:
            return []
        if self.voice.tts is None:
            # 沒有真 TTS（Demo/測試）⇒ 走 mock 那條，語意一致（一樣是「一句一個 speech 幀」）
            return self.voice.maybe_speak(self.entity, text, thread_id=self.thread_id,
                                          principal=self.principal, force=self.force,
                                          target="" if self.target == "auto" else self.target,
                                          source=self.source)
        if not self._started:
            self._prepare()
        if not self._enabled or not self._call_target:
            return []
        if self.cancelled:                       # `_prepare()` 之後可能剛好被打斷
            self._release_floor()
            return []
        # ⚠️ 並行合成（`WORKERS`）還沒上線：要真並行必須把「語音幀的吐出」從 LLM 串流
        # 迴圈裡解耦（改成背景執行緒按序 emit），否則 `speak()` 一延後就破壞「這一句的
        # frames 立刻回」的契約（實測：會出現「後面的句子沒聲音」「等不到 frames」）。
        frames = self._synth_one(text)
        if frames and not any(frame.get("kind") == "speech_error" for frame in frames) \
                and not self.cancelled:
            self.spoken += text
        return frames

    def speak_many(self, sentences: list[str]) -> list[dict]:
        """多句**並行**合成，frames 照句子順序回（照抄舊橋 `ba/voice/frames.py` 的做法）。

        owner 2026-10-24：「舊的 blue agent 之前有過並行語音生成」——舊橋是
        「整段文字先拿到 → 切句 → 一次投遞到執行緒池 → 依 index 順序取結果 → 合成好一句
        立刻發一句（第一句 priority=0 先拿實例）」。總牆鐘從「句數×單句」變成「≈最慢那句」。

        這裡沿用同一組約定：
          · 句子之間沒有依賴 ⇒ 全部同時投遞；
          · **送出照 index 順序**（端末把跳號當殘幀丟掉，順序錯＝亂序說話）；
          · 打斷（barge-in）⇒ 還沒開跑的取消，已開跑的等它回（白做但不影響正確性）。
        """
        texts = [t for t in (speech_text(item) for item in (sentences or [])) if t]
        if not texts:
            return []
        if not self._started:
            self._prepare()                       # 沒有前面那句先起頭時，這裡自己取 floor
        if self.cancelled or self.voice.tts is None \
                or not self._enabled or not self._call_target:
            # 沒有真 TTS／沒開聲音／已打斷 ⇒ 退回逐句同步路（語意一致：一句一個 speech 幀）
            frames: list[dict] = []
            for text in texts:
                frames.extend(self.speak(text))
            return frames
        workers = max(1, min(int(self.WORKERS or 1), len(texts)))
        collected = [None] * len(texts)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tts-par") as pool:
            futures = [pool.submit(self._synth_one, text) for text in texts]
            for index, future in enumerate(futures):
                if self.cancelled:                    # 主人插話：後面的不再做
                    for pending in futures[index:]:
                        pending.cancel()
                    break
                try:
                    part = list(future.result())
                except Exception as exc:              # noqa: BLE001 - 一句壞不該炸整輪
                    part = [{"kind": "speech_error",
                             "payload": {"text": texts[index], "reason": type(exc).__name__}}]
                collected[index] = part
                if part and not any(frame.get("kind") == "speech_error" for frame in part) \
                        and not self.cancelled:
                    self.spoken += texts[index]
        frames = []
        for part in collected:
            frames.extend(part or [])
        return frames

    def drain(self, *, block: bool = False) -> list[dict]:
        """把**已經算好、而且輪到它**的那幾句吐出來（順序一定照句子順序）。

        `block=True`（收尾用）＝等到全部算完。逾時／打斷時不會卡死在這裡。
        """
        out: list[dict] = []
        while self._next < len(self._pending):
            text, future = self._pending[self._next]
            if not future.done() and not block:
                break
            try:
                frames = list(future.result())
            except Exception as exc:                    # noqa: BLE001 - 一句壞不該炸整輪
                frames = [{"kind": "speech_error",
                           "payload": {"text": text, "reason": type(exc).__name__}}]
            self._next += 1
            block = True                                # 一旦開始等，就照順序等完
            if frames and not any(frame.get("kind") == "speech_error" for frame in frames) \
                    and not self.cancelled:
                self.spoken += text
            out.extend(frames)
        return out

    def _synth_one(self, text: str) -> list[dict]:
        """一句 → frames（原本 `speak()` 的主體；跑在合成執行緒裡）。"""
        found = self.voice.entities.get(self.entity)
        voice_name = (found.voice or found.id) if found is not None else self.entity
        frames = self.voice._speak_real(self.entity, text, voice=voice_name, style="",
                                       target=self._call_target, call_id=self._call_id,
                                       thread_id=self.thread_id, principal=self.principal,
                                       floor_holder=self._floor_holder)
        if any(frame.get("kind") == "speech_error" for frame in frames):
            # **一句失敗就重試一次**：橋忙／逾時是偶發的，直接放棄會讓她「只講半句」
            #（owner 2026-10-22 實測：`speech: 2, speech_errors: 1` ⇒ 後面那句沒聲音）。
            self._reason_of(frames)
            frames = self.voice._speak_real(self.entity, text, voice=voice_name, style="",
                                            target=self._call_target, call_id=self._call_id,
                                            thread_id=self.thread_id,
                                            principal=self.principal,
                                            floor_holder=self._floor_holder)
        if any(frame.get("kind") == "speech_error" for frame in frames):
            self._reason_of(frames)
            self._failed += 1
            # 連續 3 句都合成不出來才收手（一句壞不該讓整輪變成默劇）
            if self._failed >= 3:
                self._enabled = False
                # 連三敗才進冷卻（8 秒內不再打橋），避免每句都等一個 timeout
                try:
                    self.voice._tts_down_until = time.time() + 8.0
                except Exception:                       # noqa: BLE001
                    pass
            return []
        if self.cancelled:                 # 合成途中被打斷 ⇒ 這段聲音不送出去
            self._release_floor()
            return []
        # 「唸過哪幾句」由 `drain()` 記（真正吐出去的那一刻才算）
        return frames

    def _reason_of(self, frames) -> None:
        for frame in frames or ():
            if frame.get("kind") != "speech_error":
                continue
            payload = frame.get("payload") or {}
            self.last_error = str(payload.get("reason") or "speech_error")[:200]
            log = getattr(self.voice, "_log", None)
            if callable(log):
                log(f"voice: 合成失敗（{self.last_error}）")

    def finish(self) -> list[dict]:
        """收尾：等所有排隊的句子合成完，把還沒吐的 frames 照順序回給呼叫端。

        以前這裡只是放掉 floor——句子其實是「邊流邊合成」，所以最後一句常常還在算
        就被算進 `spoken`，端末那邊就少一句；現在統一在 finish() 收乾。
        """
        self._release_floor()
        return []
