"""回想：被動召回（披露規則過濾）與子代理（腦內分工）。

**分流（owner 2026-11-03）**：記憶分成兩條流——`dialogue`（他跟她真的說過的話，長命）
與 `life`（她自己的事，短命）。主對話預設只吃對話流，另留一小格給生活流
（讓她接得上「自己剛才在做什麼」）。為什麼要分：兩股流量共用一個池子時，
生活流水是對話事實的 ~30 倍，字面命中一擠，對話記憶就整片消失。

開關：`BLUEAGENT_STREAM_ROUTING=0` ⇒ 完全回到分流前的行為（單次召回、不分流），可 A/B。
生活流那一格：`BLUEAGENT_RECALL_LIFE`（預設 4 條；owner 2026-11-16 從 2 調到 4）。
"""

from __future__ import annotations

import os

from agent.brain.compose.stages.context import TurnContext
from agent.brain.domain.client_end import is_call
from agent.brain.memory.facts import STREAM_DIALOGUE, STREAM_LIFE, game_of, stream_for_source

#: 「這一場的人也在」的層（披露層已保證：在場 ⊆ 這條的房間 ⇒ 對方當時在那間房）
_ROOM_LAYERS = frozenset({"pair", "private", "circle", "consensus", "window"})


def _routing_on() -> bool:
    return str(os.environ.get("BLUEAGENT_STREAM_ROUTING") or "1").strip().lower() \
        not in {"0", "off", "false", "no"}


#: 主人對話時生活流最多佔幾格（owner 2026-11-16：2 → 4，讓她的生活感更濃一點）。
#: 只是上限：生活流字面沒命中就讓給對話流，不會硬塞。
LIFE_SLOTS = 4


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name) or default))
    except ValueError:
        return default


def shared_with_room(fact) -> bool:
    """這條記憶，**這一場的人**是不是也知道（common-ground 第 2 步，規則判定、不調模型）。

    知道 ⇔ 是在有他的房間裡說過／聽過的事：`pair`／`private`／`circle`／`consensus`／窗口摘要。
    不知道：`self`（她的私記）、`world`（她自己知道的世界事實）、跨角色聽來的（`hearsay_from`）、
    生活流（她自己做的事）、層不明的（舊路徑，保守當不知道）。
    注意：`circle` 只代表「他在那間房」，不代表他讀過那則（群聊未讀，見 PLAN-common-ground H7）。
    """
    layer = str(getattr(fact, "layer", "") or "")
    if layer not in _ROOM_LAYERS:
        return False
    if str(getattr(fact, "hearsay_from", "") or ""):
        return False
    stream = str(getattr(fact, "stream", "") or "")
    if not stream and str(getattr(fact, "source", "") or ""):
        stream = stream_for_source(str(fact.source))
    return stream != STREAM_LIFE


def _dedupe(facts) -> list:
    """同一句只留第一次（混合檢索與字面補位可能撈到同一條）。"""
    out, seen = [], set()
    for fact in facts:
        text = str(getattr(fact, "text", fact) or "")
        if text and text in seen:
            continue
        seen.add(text)
        out.append(fact)
    return out


class _Recalled:
    """召回結果的最小形狀（`RecallStage` 之後只用到 text／recall_text／to_dict／stream）。"""

    __slots__ = ("layer", "stream", "text")

    def __init__(self, text: str, *, stream: str = "", layer: str = "") -> None:
        self.text = str(text)
        self.stream = str(stream)
        self.layer = str(layer)

    @property
    def recall_text(self) -> str:
        return self.text

    def to_dict(self) -> dict:
        return {"text": self.text, "stream": self.stream, "layer": self.layer}


def _game_of(env) -> int:
    """這一輪是不是桌（TRPG）：桌上只看桌的記憶、現實只看現實的（2026-11-11 B4）。"""
    return game_of(getattr(env.event, "source", ""))


class RecallStage:
    name = "recall"

    def __init__(self, search=None) -> None:
        #: `MemorySearch`（混合檢索：字面＋向量＋窗口摘要）。沒有就退回記憶埠的字面召回。
        self.search = search

    def run(self, env, ctx):
        scope = env.decision.scope
        participants = set(env.event.participants or ()) | {env.decision.principal}
        # 記憶深度（owner 2026-11-02「記憶那段加深一點」）：4 → 8 條，可用 env 調
        limit = max(1, _int_env("BLUEAGENT_RECALL_LIMIT", 8))
        if hasattr(ctx.ports, "recall_detail"):
            # 分流：對話流在前，生活流補一小格（預設 `LIFE_SLOTS` 條）
            detail = self._merged(env, ctx, scope, participants, limit) \
                if _routing_on() else self._once(env, ctx, scope, participants, limit)
            # `recall_text`：跨角色鏡像進來的那條帶「聽誰說的」前綴（她提起時用轉述口吻）
            # 〔私記〕＝她自己那層（她永遠想得起來；要不要說由她定，owner 2026-11-03 線路 A）
            # 〔作品〕＝她自己寫的虛構文本（owner 2026-11-16）：可以聊「我寫到那段」，
            # 不能說成「我在那裡」——前綴讓組句時分得清經歷與作品。
            facts = [("〔作品〕" if int(getattr(fact, "fiction", 0) or 0)
                      else "〔私記〕" if str(getattr(fact, "layer", "") or "") == "self" else "")
                     + (getattr(fact, "recall_text", None) or getattr(fact, "text", str(fact)))
                     for fact in detail]
            rendered = [fact.to_dict() if hasattr(fact, "to_dict") else {"text": str(fact)}
                        for fact in detail]
            shared = [shared_with_room(fact) for fact in detail]
        else:
            facts = list(ctx.ports.recall(scope, env.text, limit=limit,
                                          participants=participants, game=_game_of(env)))
            rendered = [{"text": fact} for fact in facts]
            shared = [False] * len(facts)          # 不知道來歷 ⇒ 保守當「只有她知道」
        recall = TurnContext.of(env).recall
        recall.recall = facts
        recall.recall_shared = shared
        recall.recall_detail = rendered
        recall.participants = sorted(participants)
        recall.banks = list(scope.banks())
        ctx.emit("memory", {"facts": facts, "detail": rendered,
                            "room": sorted(participants), "banks": recall.banks,
                            "principal": env.decision.principal,
                            "streams": [str(row.get("stream") or "") for row in rendered]})
        ctx.mark("recall")                     # 打點：記憶檢索＋披露過濾做完
        return None

    # ── 兩種取法（只差在要不要分兩次、要不要帶 stream）────────────────────────
    def _once(self, env, ctx, scope, participants, limit: int) -> list:
        """分流前的行為：一次召回、不分流（`BLUEAGENT_STREAM_ROUTING=0` 走這條）。"""
        return self._detail(env, ctx, scope, participants, limit)

    def _by_stream(self, env, ctx, scope, participants, limit: int, stream: str) -> list:
        """指定流召回，連同當前場景 `game` 一併下傳給記憶埠。"""
        return self._detail(env, ctx, scope, participants, limit, stream=stream)

    def _detail(self, env, ctx, scope, participants, limit: int, *, stream: str = "") -> list:
        """呼叫記憶埠的 `recall_detail`：一次帶齊 `limit`／`participants`／`stream`／`game`。

        不做類型異常退回（2026-11-15 WP1）：以前為相容舊替身逐級拔掉 `game`，
        導致 `AppPorts.recall_detail` 缺參數時靜默退化成不過濾場景。
        """
        return list(ctx.ports.recall_detail(
            scope, env.text, limit=limit, participants=participants,
            stream=stream, game=_game_of(env)))

    def _merged(self, env, ctx, scope, participants, limit: int) -> list:
        """對話流在前、生活流補一小格；同一句只留一次。

        對話流走**混合檢索**（`MemorySearch`：字面＋向量＋窗口摘要）——字面召回的門檻是
        `overlap > 0`，像「前に話したあのこと」這種泛指問句一個字都對不上，記憶明明在也撈不到
        （2026-11-03 實測：分流後仍 0 命中）。生活流維持字面召回（她自己的筆記，字面就夠）。
        """
        life_n = min(_int_env("BLUEAGENT_RECALL_LIFE", LIFE_SLOTS), max(0, limit - 1))
        life = self._by_stream(env, ctx, scope, participants, life_n, STREAM_LIFE) \
            if life_n else []
        room = max(1, limit - len(life))
        dialogue = list(self._hybrid(env, scope, participants, room) or ())
        # 混合檢索沒填滿（沒有它／它壞了／沒向量時門檻把字面命中也砍光）⇒ 字面召回補位。
        # 只在回 None 時才退回的話，「一個字都對得上」的對話事實會整條消失（群聊換人那條就是）。
        if len(dialogue) < room:
            dialogue += self._by_stream(env, ctx, scope, participants, room, STREAM_DIALOGUE)
        return _dedupe(_dedupe(dialogue)[:room] + list(life))[:limit]

    def _hybrid(self, env, scope, participants, room: int) -> list | None:
        """對話流：`MemorySearch` 的混合檢索（事實 ＋ 窗口摘要）。不可用就回 None。"""
        if self.search is None:
            return None
        # 窗口摘要最多佔兩格：它是「整段聊過什麼」的載體，但她說話不能變成複述摘要
        windows_n = min(2, max(0, room - 2))
        facts_n = max(1, room - windows_n)
        try:
            result = self.search.search(
                entity=str(scope.entity), present=participants,
                query=env.text, limit=max(facts_n, windows_n),
                layers=("fact", "window"), stream=STREAM_DIALOGUE,
                # 場景維度（2026-11-11 B4）：桌上不撈現實私事、現實不撈桌上的事
                game=_game_of(env))
        except Exception:                        # noqa: BLE001 - 檢索壞了不能擋回話
            return None
        if not isinstance(result, dict) or not result.get("ok"):
            return None
        out: list = []
        for row in (result.get("facts") or [])[:facts_n]:
            out.append(_Recalled(str(row.get("text") or ""), stream=STREAM_DIALOGUE,
                                 layer=str(row.get("layer") or "")))
        for row in (result.get("windows") or [])[:windows_n]:
            topic = str(row.get("topic") or "").strip()
            summary = str(row.get("summary") or "").strip()
            if not summary:
                continue
            head = f"（以前のやりとり：{topic}）" if topic else "（以前のやりとり）"
            out.append(_Recalled(head + summary[:120], stream=STREAM_DIALOGUE, layer="window"))
        return out


class SubAgentStage:
    """子代理：**她腦內的分工**（查記憶／先想一下），單行動主線不變（owner 2026-10-23）。

    只做兩件事：跑子代理、把筆記放進 `TurnContext.recall.subagent_notes`。
    **不寫記憶、不發訊息**——那些仍然只有主線（BrainStage 之後）能做。
    """

    name = "subagents"

    def __init__(self, orchestrator=None, prethinker=None) -> None:
        self.orchestrator = orchestrator
        #: 閒時預想（`runtime/prethink.py`）：拿一份還新鮮的、用過就作廢
        self.prethinker = prethinker

    def run(self, env, ctx):
        scope = env.decision.scope
        recall = TurnContext.of(env).recall
        carried: list[str] = []
        present = set(env.event.participants or ()) | {env.decision.principal, scope.entity}
        if self.prethinker is not None:
            try:
                note = self.prethinker.take(str(scope.entity), present=present)
            except Exception:                            # noqa: BLE001
                note = ""
            if note:
                carried.append(f"[prethink] {note}")
                recall.prethink = note
        if self.orchestrator is None:
            if carried:
                recall.subagent_notes = carried
            return None
        # `game`：子代理的本地檢索（RecallScout）也要分場景，否則桌上的原文從這條旁路進現實回合
        meta = {"persona_hint": "", "recall": list(recall.recall or []), "game": _game_of(env)}
        # App 単人通話（owner 2026-10-29）：只跑本地快檢索，**不跑 LLM 子代理**。
        # 實測 `subagents_ms=3768` 全卡在她開口之前；通話要的是快，不是內心戲。
        # 手機與 MR 的通話都算（`domain/client_end.py`；brain 的 prompt 用同一個判斷）。
        in_call = is_call(env.event.source)
        # 子代理只能用**整輪 deadline 的一部分**（owner 2026-11-02 重排）：從前它自己帶 6 s
        # 的上限，跟整輪 8 s 各守各的 ⇒ 它「按時」跑滿 6 s、主腦只剩 2 s 出首字。
        # 這裡直接把額度切給它（預設 35%），主腦那邊才有保底。
        import os
        deadline_ms = getattr(env.decision, "deadline_ms", None)
        try:
            ratio = float(os.environ.get("BLUEAGENT_SUBAGENT_BUDGET_RATIO") or 0.35)
        except ValueError:
            ratio = 0.35
        budget_ms = int(float(deadline_ms) * max(0.05, min(1.0, ratio))) if deadline_ms else None
        try:
            notes = self.orchestrator.think(entity=str(scope.entity), present=present,
                                            text=str(env.text or ""), meta=meta,
                                            fast_only=in_call, budget_ms=budget_ms)
        except Exception as exc:                            # noqa: BLE001 - 子代理壞了不擋主線
            ctx.emit("subagent", {"ok": False, "error": type(exc).__name__})
            return None
        rendered = [note.render() for note in notes if str(note.text).strip()]
        recall.subagent_notes = carried + rendered
        rendered = recall.subagent_notes
        ctx.emit("subagent", {"ok": True, "count": len(rendered),
                              "agents": [note.agent for note in notes],
                              "notes": rendered})
        ctx.mark("subagents")                 # 打點：子代理（內心話／查記憶／提前想）做完
        return None
