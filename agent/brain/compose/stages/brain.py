"""大腦階段：串流、工具子回合（最多三輪）、首句先合成、文字先發。

review.md（2026-10-25）P2：原本 `BrainStage.run()` 一個 232 行的函式，同時做請求構造、
工具迴圈、取消、分段、TTS 串流與收尾，而且全靠 `env.meta["…"]` 字串拿資料。現在：
  · 讀寫都走 `TurnContext`（`app/stages/context.py` 宣告了每個鍵的預設值與寫入者）；
  · 請求構造 `build_request`、工具執行 `_run_tool`、語音串流 `_Speech`、分段 `_Bubbles`、
    收尾 `_finish` 各自一塊；`run()` 只剩每一輪的流程。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace

from agent.brain.compose.stages.context import TurnContext
from agent.brain.domain.client_end import is_call
from agent.brain.memory.facts import game_of
from agent.brain.compose.stages.speech_text import _SENTENCE_END, _take_sentences
from agent.brain.ports import BrainRequest
from agent.brain.runtime.memory import usage_feedback
from agent.brain.runtime.tools import ToolContext, ToolRegistry
from agent.brain.runtime.tools.channels import channel_of
from agent.brain.runtime.voice import VoiceService


#: 工具调用是一轮内的子回合：执行完把结果喂回去，最多三轮，防止模型无限调用。
MAX_ROUNDS = 3


def _current_game_block() -> str:
    """目前進行中的遊戲段（沒有就空字串）。"""
    try:
        from agent.brain.runtime.game_context import current_block
        return current_block()
    except Exception:                                   # noqa: BLE001
        return ""


def _emit_frames(ctx, frames, default_kind: str = "speech") -> None:
    for frame in frames:
        ctx.emit(frame.get("kind", default_kind), dict(frame.get("payload") or {}))


def _note(ctx, name: str, value) -> None:
    """這一輪的量測值（只進 telemetry，不發給端末）；sink 不支援就算了。"""
    noter = getattr(getattr(getattr(ctx, "sink", None), "__self__", None), "note", None)
    if callable(noter):
        try:
            noter(name, value)
        except Exception:                               # noqa: BLE001 - 量測不能擋回話
            pass


class _Speech:
    """邊生成邊合成（首句優先）。speaker **延後到第一次要出聲時才建**：`voice.speak` 這種工具
    是在文字之前才把 `force_voice` 設起來的，提前建會拿到舊值（2026-10-22 踩到：整輪沒聲音）。"""

    def __init__(self, voice, env, turn: TurnContext) -> None:
        self.voice, self.env, self.turn = voice, env, turn
        self.speaker = None
        self.buffer = ""
        self.deferred: list[str] = []
        self.first_spoken = False

    def new_round(self) -> None:
        # 每一輪工具子回合重新算「首句」與排隊（speaker 與緩衝跨輪保留，跟拆之前一樣）
        self.deferred = []
        self.first_spoken = False

    def feed(self, ctx, piece: str) -> None:
        self.buffer += piece
        for sentence in _take_sentences(self.buffer):
            self.buffer = self.buffer[len(sentence):]
            if self.speaker is None:
                env = self.env
                self.speaker = self.voice.begin_stream(
                    env.decision.entity, thread_id=env.thread.id,
                    principal=env.decision.principal,
                    force=bool(self.turn.output.force_voice),
                    target=str(self.turn.output.voice_target or ""),
                    source=str(env.event.source))
            # 首句優先：第一句一完整就合成送出（這才是「開始出聲」的延遲）。後面的句子**先排隊**，
            # 等模型吐完再一次並行（舊橋的做法：總時間≈最慢那句，而不是句數×單句）。
            if not self.first_spoken:
                _emit_frames(ctx, self.speaker.speak(sentence))
                self.first_spoken = True
            else:
                self.deferred.append(sentence)
            if self.speaker.cancelled:
                # 主人已經插話（barge-in）：剩下的句子**不再合成**，但文字照常流給端末
                self.turn.output.voice_interrupted = True
                break

    def finish(self, ctx) -> None:
        """`done` 之後才收尾語音（文字先發）：緩衝裡最後一句＋並行合成剩下的部分。"""
        if self.speaker is None:
            return
        tail = self.buffer.strip()
        self.buffer = ""
        pending = list(self.deferred) + ([tail] if tail else [])
        if pending and not self.speaker.cancelled:
            # 句子之間沒有依賴，同時投遞；frames 照順序回來，所以聽到的仍是第 2、3、4 句的順序
            _emit_frames(ctx, self.speaker.speak_many(pending))
        _emit_frames(ctx, self.speaker.finish())
        self.turn.output.spoken_text = self.speaker.spoken


class _Bubbles:
    """回覆分段（server 側唯一一份，與 TTS 的切句同一個句柄）。"""

    def __init__(self) -> None:
        from agent.brain.runtime import reply_split
        self.split = reply_split
        self.text = ""
        self.emitted = 0

    def feed(self, ctx, piece: str) -> None:
        """最後一條還在長，只推到倒數第二條。"""
        if not piece:
            return
        self.text += piece
        groups = self.split.groups(self.text)
        ready = self.split.ready_count(groups, final=False)
        while self.emitted < ready:
            ctx.emit("part", {"index": self.emitted, "text": groups[self.emitted],
                              "final": False})
            self.emitted += 1

    def finalize(self, ctx, reply: str) -> list[str]:
        """定稿：剩下的氣泡一次補齊（含最後一條），回全文分段。"""
        groups = self.split.groups(reply) if reply else []
        while self.emitted < len(groups):
            ctx.emit("part", {"index": self.emitted, "text": groups[self.emitted],
                              "final": self.emitted == len(groups) - 1})
            self.emitted += 1
        return groups


class _Motion:
    """她**開口時**就發 `motion` 幀（計畫 §5.4），一輪最多一條。

    每長出一個完整的句子就拿「到目前為止說完的部分」比規則；命中才去挑片段（`library.pick` 會輪替，
    不能每句都叫）。整輪都沒命中 ⇒ `finish()` 拿定稿全文再試一次。
    以前發在語音出口（一輪的尾巴）⇒ 實機上她**先講完才動**（2026-10-31）。
    """

    def __init__(self, planner, env, turn: TurnContext) -> None:
        self.planner, self.env, self.turn = planner, env, turn
        self.text = ""
        self.checked = 0

    def feed(self, ctx, piece: str) -> None:
        if self.planner is None or self.turn.output.motion_sent or not piece:
            return
        self.text += piece
        end = max(self.text.rfind(mark) for mark in _SENTENCE_END)
        if end + 1 > self.checked:
            self.checked = end + 1
            self._try(ctx, self.text[:end + 1])

    def finish(self, ctx, reply: str) -> None:
        if self.planner is not None and not self.turn.output.motion_sent:
            self._try(ctx, reply)

    def chosen(self, ctx, tag: str) -> None:
        """她自己在回覆開頭標的動作（`[[motion:greet]]`）：優先於規則表，標了就照它。"""
        if self.planner is not None and not self.turn.output.motion_sent and tag:
            self._try(ctx, "", tag=tag)

    def _try(self, ctx, said: str, tag: str = "") -> None:
        from agent.brain.compose.motion import tag_for
        if not tag and not tag_for(said):
            return
        env = self.env
        try:
            frame = self.planner.frame(entity=env.decision.entity, text=said, tag=tag,
                                       source=str(getattr(env.event, "source", "") or ""))
        except Exception as exc:                     # noqa: BLE001 — 動作壞了不能拖垮這一輪
            print(f"motion: 挑動作失敗（{exc!r}）⇒ 這輪沒有動作", flush=True)
            frame = None
        # 規則命中但沒發（來源不是舞台／庫裡沒有）⇒ 也算這輪結束，免得每句都再挑一次
        self.turn.output.motion_sent = True
        if frame:
            ctx.emit("motion", dict(frame))
            _note(ctx, "motion", f"{'chosen' if tag else 'rule'}:{frame.get('tag')}")   # telemetry：誰挑的
            print(f"motion: {frame.get('tag')} → {frame.get('id')}（{frame.get('seconds')}s）", flush=True)


class _TagFilter:
    """串流裡把 `[[motion:…]]` 抽走（owner 2026-10-31：動作讓模型自己挑）。

    標籤可能被切在兩塊 delta 中間 ⇒ 看到 `[` 就先扣住，湊齊 `]]`（或確定不是標籤）才放行。
    放行的文字才進語音、氣泡、端末；抽到的 tag 交給 `on_tag`。
    """

    PREFIX = "[[motion:"
    MAX_HOLD = 32

    def __init__(self, on_tag) -> None:
        self.on_tag = on_tag
        self.held = ""

    def feed(self, piece: str) -> str:
        text = self.held + str(piece or "")
        self.held = ""
        kept: list[str] = []          # ⚠️ 不能叫 out：檔內 `out = turn.output`，契約測試按名字認別名
        while text:
            start = text.find("[")
            if start < 0:
                kept.append(text)
                break
            kept.append(text[:start])
            rest = text[start:]
            if self.PREFIX.startswith(rest[:len(self.PREFIX)]) and len(rest) < len(self.PREFIX):
                self.held = rest                        # 可能是標籤的開頭：等下一塊
                break
            if not rest.startswith(self.PREFIX):
                kept.append(rest[0])
                text = rest[1:]
                continue
            end = rest.find("]]")
            if end < 0:
                if len(rest) > self.MAX_HOLD:           # 太長了不會是標籤 ⇒ 照文字放行
                    kept.append(rest)
                else:
                    self.held = rest
                break
            self.on_tag(rest[len(self.PREFIX):end].strip().lower())
            text = rest[end + 2:]
        return "".join(kept)

    def flush(self) -> str:
        held, self.held = self.held, ""
        return held


class BrainStage:
    name = "brain"

    def __init__(self, tools: ToolRegistry, voice: VoiceService, motion=None) -> None:
        self.tools = tools
        self.voice = voice
        #: 動作規劃器（`app/motion.py`）；None＝這一版不發 `motion` 幀
        self.motion = motion

    # ── 請求 ────────────────────────────────────────────────────────────────
    def tool_schemas(self, env, turn: TurnContext, unlocked=()) -> list[dict]:
        # `quick`（語音先短回一句）：不給工具、不委任——只求「馬上出聲」
        # ⚠ 2026-10-22：原本讀 meta 的 `quick`，但它從來沒被 payload 填過 ⇒ 短回覆一直帶著整套工具
        if env.event.payload.get("quick"):
            return []
        # 分層（`runtime/tools/channels.py`）：渠道不對的不給；按需工具要這一輪載入過技能才給
        return self.tools.schemas(mode=str(turn.persona.mode or "chat"),
                                  is_owner=bool(turn.persona.is_owner),
                                  capabilities=turn.persona.capabilities or (),
                                  entity=env.decision.entity, source=str(env.event.source),
                                  unlocked=unlocked)

    def _unlock(self, env, turn: TurnContext, ctx, request: BrainRequest, calls: list[dict],
                unlocked: set) -> BrainRequest:
        """這一步若成功 `skill.view` 了某個技能，把它 `requires: [tool:…]` 的工具放進後面步驟的清單。"""
        grew = False
        for call in calls:
            result = call.get("result") or {}
            if self.tools.canonical(str(call.get("name") or "")) != "skill.view" or not result.get("ok"):
                continue
            for item in (result.get("data") or {}).get("requires") or ():
                text = str(item)
                if not text.startswith("tool:"):
                    continue
                name = self.tools.canonical(text[len("tool:"):].strip())
                if name and name not in unlocked:
                    unlocked.add(name)
                    grew = True
        if not grew:
            return request
        tools = self.tool_schemas(env, turn, unlocked)
        _note(ctx, "tools_unlocked", sorted(unlocked))
        return replace(request, meta={**request.meta, "tools": tools})

    def build_request(self, env, turn: TurnContext) -> BrainRequest:
        persona = turn.persona
        return BrainRequest(
            thread_id=env.thread.id, entity=env.decision.entity,
            principal=env.decision.principal, text=env.text,
            system=persona.persona,
            mode=str(persona.mode or "chat"), want_voice=False,
            deadline_ms=env.decision.deadline_ms,
            meta={
                "recall": turn.recall.recall,
                "recall_shared": turn.recall.recall_shared,
                "group": persona.group,
                "ownerless": persona.ownerless,
                "addressed_to": env.event.addressed_to,
                "entity_name": (persona.entity_name if persona.has("entity_name")
                                else env.decision.entity),
                "principal_name": (persona.principal_name if persona.has("principal_name")
                                   else env.decision.principal),
                "names": persona.names,
                "transcript": persona.transcript,
                "stance": persona.stance,
                "soul_block": persona.soul_block,
                # 這個人的那一頁（檔案層，每一輪都進）—— 忘了帶就等於這一層不存在
                "user_block": persona.user_block,
                "self_log_block": persona.self_log_block,
                "mood_block": persona.mood_block,
                # in-time 現況（她此刻在做什麼）與場上其他人的背景（owner 2026-11-03）
                "presence_block": persona.presence_block,
                "ambient": persona.ambient,
                "room_note": persona.room_note,
                # 承接段（上一段封片的摘要 ⇒ `[[before]]`）：以前寫進 persona 卻沒帶過來，`[[before]]` 從沒出現過
                "carry_summary": persona.carry_summary,
                # 跨線餘溫（D2）：她別條活線剛剛的幾輪，帶渠道來源 ⇒ `[[elsewhere]]`
                "elsewhere": persona.elsewhere,
                # 未渡しの依頼（2026-11-08）：主人要的交付還沒交 ⇒ `[[todo]]`
                "delivery_block": persona.delivery_block,
                # 時間感（owner 2026-10-27）：現在時刻／這條線第幾輪／心跳跳數
                "clock_block": persona.clock_block,
                "now": time.time(),
                "speak_lang": persona.speak_lang,
                "tools": self.tool_schemas(env, turn),
                # 3D 舞台的動作（owner 2026-10-31）：她可以在回覆開頭標 `[[motion:…]]`；沒有舞台 ⇒ 空
                "motion_tags": (self.motion.tags(str(env.event.source)) if self.motion is not None else []),
                "tool_trace": [],
                "core_block": persona.core_block,
                "state_block": turn.state.state_block,
                # 遊戲段（TRPG 等）獨立一段、不吃滾動預算；局終就回收
                "game_block": turn.overrides.game_block or _current_game_block(),
                # 「先短回一句 → 再補完整答案」的第二段：帶上剛剛說過的話，避免開頭重複
                "followup": bool(env.event.payload.get("followup")),
                "ack_text": str(env.event.payload.get("ack_text") or ""),
                # App 的單人通話（owner 2026-10-29）：回覆是講出來的 ⇒ prompt 要她講短、講口語
                # App 單人通話（手機 `phone:call`／MR `mr:call`；`/api/audio` 帶 `X-Call` 時由 HTTP 層標）
                "in_call": is_call(env.event.source),
                # 這一回從哪個端來（`app`／`discord`／`discord_vc`／`call`／`trpg`）：決定要不要附 `[[form]]`
                "channel": channel_of(str(env.event.source or "")),
                # 本輪段用哪種角色送（ADR-0020）：能吃中途 system 的服務用 system（預設）；
                # llama-server／Qwen 模板會 500「System message must be at the beginning」⇒ 設 user
                "turn_blocks_role": os.environ.get("BLUEAGENT_TURN_BLOCKS", "system"),
            },
        )

    def _stream_voice(self, turn: TurnContext) -> bool:
        """首句優先（現役橋的做法）：邊生成邊合成，首句延遲＝首句生成＋首句合成，不用等整段。"""
        if self.voice is None:
            return False
        wanted = turn.overrides.tts_stream
        if not turn.overrides.has("tts_stream"):
            wanted = os.environ.get("BLUEAGENT_TTS_STREAM", "1").strip().lower() \
                not in {"0", "off", "false", "no"}
        return bool(wanted)

    # ── 工具 ────────────────────────────────────────────────────────────────
    def _run_tool(self, env, turn: TurnContext, ctx, payload, call_id: str) -> dict:
        name = str(payload.get("name") or "")
        arguments = payload.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        result = self.tools.run(
            name, arguments,
            ToolContext(entity=env.decision.entity, principal=env.decision.principal,
                        thread_id=env.thread.id, source=env.event.source, voice=self.voice,
                        mode=str(turn.persona.mode or "chat"),
                        is_owner=bool(turn.persona.is_owner),
                        capabilities=turn.persona.capabilities or frozenset(),
                        present=frozenset(env.event.participants or ())),
        )
        # 一行痕跡：她這一輪到底叫了哪支工具（2026-11-04 實測「三輪都只叫工具、一個字都沒寫」
        # 的空回，沒有這行就只能靠猜）
        print(f"[tool] {name} ok={bool(result.ok)} err={str(result.error or '')[:48]}", flush=True)
        out = turn.output
        out.add_tool_result(result.to_dict())
        # 效用記帳（owner 2026-10-24）：這一輪撈出來的**事實**先記下來，
        # 等回覆出來再判「有沒有真的用到」（MemoryUsageStage）。
        facts_hit = [str((row or {}).get("text") or "")
                     for row in ((result.data or {}).get("facts") or [])]
        facts_hit = [text for text in facts_hit if text.strip()]
        if facts_hit:
            out.add_memory_hits(facts_hit)
        _emit_frames(ctx, result.frames, default_kind="tool")
        if result.ok and result.data.get("sticker"):
            out.add_sticker(str(result.data.get("meaning") or result.data.get("sticker")),
                            sticker_id=str(result.data.get("sticker") or ""))
        if result.data.get("force_voice"):
            out.force_voice = True
            out.voice_target = str(result.data.get("target") or "")
        if not result.ok and result.error:
            out.tool_error = result.error
        return {"id": call_id, "name": name, "arguments": arguments, "result": result.to_dict()}

    @staticmethod
    def _with_trace(request: BrainRequest, calls: list[dict]) -> BrainRequest:
        """這一輪的工具呼叫與結果接進 trace，下一輪模型看得到（名字保持模型叫的那個）。"""
        trace = list(request.meta.get("tool_trace") or ())
        trace.append({
            "calls": [{"id": call["id"], "name": call["name"],
                       "arguments": json.dumps(call["arguments"], ensure_ascii=False)}
                      for call in calls],
            "results": [{"call_id": call["id"],
                         "content": json.dumps(call["result"], ensure_ascii=False)}
                        for call in calls],
        })
        return replace(request, meta={**request.meta, "tool_trace": trace})

    # ── 一輪 ────────────────────────────────────────────────────────────────
    def run(self, env, ctx):
        turn = TurnContext.of(env)
        request = self.build_request(env, turn)
        self._trace_trpg(env, turn, request)
        speech = _Speech(self.voice, env, turn) if self._stream_voice(turn) else None
        bubbles = _Bubbles()
        unlocked: set[str] = set()
        offered = request.meta.get("tools") or []
        # 量測（owner 2026-10-31 分層）：這一輪一開始給了幾支工具、多少字 ⇒ turns.jsonl
        _note(ctx, "tools", len(offered))
        _note(ctx, "tool_chars", len(json.dumps(offered, ensure_ascii=False)))
        motion = _Motion(self.motion, env, turn)
        tags = _TagFilter(lambda tag: motion.chosen(ctx, tag))
        ctx.mark("brain_start")               # 打點：真的開始跟模型講話（之前全是前置）
        for round_index in range(MAX_ROUNDS):
            # **步邊界停**（owner 2026-10-22）：不硬中斷，只在「一輪工具回合」之間停。
            # 中斷旗標由調度器的協作式 preempt 翻起來，這裡是它唯一的落地點之一。
            if ctx.cancel.cancelled:
                ctx.emit("interrupted", {"at": f"brain:round{round_index}", "reason": "cancelled"})
                return
            if speech is not None:
                speech.new_round()
            calls: list[dict] = []
            text = ""
            final_payload = None
            for kind, payload in ctx.ports.run_turn(request):
                # 串流中也是邊界：收到取消就停止消費（模型端自然收尾，不殺執行緒）
                if ctx.cancel.cancelled:
                    ctx.emit("interrupted", {"at": "brain:stream", "reason": "cancelled"})
                    return
                if kind == "tool_call":
                    call_id = str(payload.get("id") or f"call_{round_index + 1}_{len(calls) + 1}")
                    calls.append(self._run_tool(env, turn, ctx, payload, call_id))
                elif kind == "done":
                    text = str(payload.get("text") or "").strip()
                    final_payload = dict(payload)
                else:
                    if kind == "delta":
                        piece = tags.feed(str(payload.get("text") or ""))   # 抽走動作標籤（不唸、不顯示）
                        if not piece:
                            continue
                        payload = {**payload, "text": piece}
                        motion.feed(ctx, piece)      # 先於語音：動作幀比首句音檔早到
                        if speech is not None:
                            speech.feed(ctx, piece)
                        bubbles.feed(ctx, piece)
                        # ⚠️ **不再把 delta 送給端末**（owner 2026-11-04「我們的 sse 不再需要 delta 幀」）：
                        # 她是「一條一條短訊息」的角色，逐字吐出來的觀感是錯的。文字只走 `part`
                        # （server 切好的氣泡），音檔只走 `speech`。delta 只留在管線內部餵切句／動作。
                        continue
                    ctx.emit(kind, dict(payload))
            leftover = tags.flush()                   # 串流收尾：扣住等標籤的那一小段（不是標籤）放行
            if leftover:
                motion.feed(ctx, leftover)
                if speech is not None:
                    speech.feed(ctx, leftover)
                bubbles.feed(ctx, leftover)
            if calls:
                request = self._with_trace(request, calls)
                request = self._unlock(env, turn, ctx, request, calls, unlocked)
            if calls and not text and round_index < MAX_ROUNDS - 1:
                continue
            if calls and not text:
                # 工具回合用完還沒吐出字：**最後再要一句話**（不給工具），讓她老實報告
                # 做了什麼／卡在哪。以前這裡直接靜默結束，端末只看到「她沒反應」（2026-11-08 實測）。
                text = self._report_without_tools(env, turn, ctx, request, speech, bubbles, motion)
            self._finish(env, turn, ctx, text, final_payload,
                         loop_limit=bool(calls and not text),
                         speech=speech, bubbles=bubbles, motion=motion)
            return

    def _report_without_tools(self, env, turn, ctx, request, speech, bubbles, motion) -> str:
        """最後一問：`tools=[]` ＋ `[[tool_limit]]`，只要一句報告（失敗就回空，不擋收尾）。"""
        from dataclasses import replace
        meta = dict(request.meta)
        meta["tools"] = []
        meta["tool_limit"] = True
        final = ""
        try:
            for kind, payload in ctx.ports.run_turn(replace(request, meta=meta)):
                if kind == "done":
                    final = str(payload.get("text") or "").strip()
                elif kind == "delta":
                    piece = str(payload.get("text") or "")
                    if piece:
                        motion.feed(ctx, piece)
                        if speech is not None:
                            speech.feed(ctx, piece)
                        bubbles.feed(ctx, piece)
                elif kind == "error":
                    _note(ctx, "tool_limit_error", str(payload.get("error") or ""))
        except Exception as exc:                       # noqa: BLE001 - 收尾失敗就照舊靜默
            print(f"  brain[{env.decision.entity}] 收尾報告失敗（{type(exc).__name__}）", flush=True)
            return ""
        return final

    def _finish(self, env, turn: TurnContext, ctx, reply: str, final_payload, *,
                loop_limit: bool, speech, bubbles: _Bubbles, motion: _Motion) -> None:
        # ⚠️ **回覆正文要清掉「不是話」的部分**（2026-10-29 owner）：
        # · `#112（いま）` —— 畫面用的編號標記，模型有時會照抄進回覆（TTS 會念數字）
        # · `（スタンプ：えへへ）` —— 假貼圖；真的貼圖走 `sticker` 幀（畫面上的圖）
        # 規則在 `runtime/text_clean.py`（顯示與唸出共用）。
        from agent.brain.runtime.text_clean import clean_reply
        reply = clean_reply(reply)
        turn.output.reply_text = reply
        # 她送了表情 ⇒ 寫進這條線（下一輪看得到「我剛剛回了哪張」）；端末那邊另有 `sticker` 幀。
        # ⚠️ 寫成**標籤** `[[stamp_out:意思]]`，不寫「（スタンプ：意思）」（複檢 2026-10-29）：
        # 那正是不准她寫進正文的假貼圖格式，記在她自己的歷史裡等於每輪示範給她看。
        said = " ".join([reply] + [f"[[stamp_out:{meaning}]]"
                                   for meaning in turn.output.stickers or ()]).strip()
        if said:
            ctx.record("assistant", said)
        payload = final_payload or {"text": reply, "entity": env.decision.entity,
                                    "principal": env.decision.principal, "model": "brain"}
        payload["text"] = reply
        # 她這一輪發的表情 id ⇒ outbox 的正文會附上英文標記 `[[sticker:<id>]]`
        # （owner 2026-11-04：用英文記號標注表情包，前台渲染最省事）
        if turn.output.sticker_ids:
            payload["sticker_ids"] = list(turn.output.sticker_ids)
        try:
            groups = bubbles.finalize(ctx, reply)
            if groups:
                payload["bubbles"] = groups
        except Exception:                       # noqa: BLE001 - 分段壞了不能擋回覆
            pass
        motion.finish(ctx, reply)
        if loop_limit:
            payload["error"] = "tool_loop_limit"
        self._note_usage(ctx, payload.get("usage") or {})
        ctx.mark("llm_done")             # 打點：模型這一段吐完（之後是收尾／合成）
        # ── 文字先發（owner 2026-10-24）：`done` 先吐出去，端末拿到文字就貼，不等語音合成
        ctx.emit("done", payload, final=True)
        # 記憶使用量回饋（α/β/γ 標定的真值）：出字之後才記帳；召回清單在 env.meta，不在 ctx 上
        usage_feedback.mark(ctx, reply, recall=turn.recall.recall, entity=env.decision.entity,
                            game=game_of(getattr(env.event, "source", "")))
        if speech is not None:
            speech.finish(ctx)

    @staticmethod
    def _note_usage(ctx, usage: dict) -> None:
        """把這一輪的 token 帳記進 telemetry（`data/logs/turns.jsonl`）。

        為什麼要記**快取命中**（owner 2026-11-01）：ADR-0020 的整套排法（本輪段放歷史之後、
        窗口按檔位收縮）唯一能驗的數字就是它。以前 `usage` 一路帶到這裡就丟了，
        「命中率到底多少」只能靠外面的實驗猜。
        DeepSeek 用 `prompt_cache_hit_tokens`／`prompt_cache_miss_tokens`；
        別家可能只有其中一個，缺的就不寫（不填 0，免得看板把它當真的 0 命中）。
        """
        if not isinstance(usage, dict) or not usage:
            return
        prompt = usage.get("prompt_tokens")
        hit = usage.get("prompt_cache_hit_tokens")
        miss = usage.get("prompt_cache_miss_tokens")
        if isinstance(prompt, (int, float)):
            _note(ctx, "llm_prompt_tokens", int(prompt))
        if isinstance(hit, (int, float)):
            _note(ctx, "llm_cache_hit", int(hit))
        if isinstance(miss, (int, float)):
            _note(ctx, "llm_cache_miss", int(miss))
        if isinstance(hit, (int, float)) and isinstance(prompt, (int, float)) and prompt:
            _note(ctx, "llm_cache_rate", round(float(hit) / float(prompt), 3))

    @staticmethod
    def _trace_trpg(env, turn: TurnContext, request: BrainRequest) -> None:
        """TRPG 取證：把「這一輪實際送進 brain 的東西」印出來（長度/工具數/期限）。

        ⚠ 工具數以前讀 `env.meta["tools"]`——那個鍵從來沒人寫（工具在 request.meta 裡），
        所以這行一直印 tools=0（2026-10-25 契約測試抓到）。現在讀真的送出去的那份。
        """
        if not str(env.event.source).startswith("trpg"):
            return
        try:
            print(f"  trpg[brain] source={env.event.source} "
                  f"system={len(turn.persona.persona or '')}字 "
                  f"text={len(env.text or '')}字 "
                  f"transcript={len(turn.persona.transcript or [])}條 "
                  f"tools={len(request.meta.get('tools') or [])} "
                  f"deadline={env.decision.deadline_ms}ms "
                  f"mode={turn.persona.mode} engage={env.decision.engage}",
                  flush=True)
        except Exception:                           # noqa: BLE001
            pass


__all__ = ["BrainStage", "MAX_ROUNDS", "_current_game_block"]
