"""狀態引出（SPIN 第②段）：這一輪的心情／狀態 → 情緒量表。"""

from __future__ import annotations

import json
import pathlib
import time

from agent.brain.compose.stages.context import TurnContext
from agent.brain.persona.state_elicit import elicit_state, prompt_block as state_block_of
from agent.brain.persona.state_elicit import should_elicit


class StateElicitStage:
    """SPIN 第 ② 段：用 core × 當前情境推出 transient state（**只在群聊／多人／TRPG 跑**）。

    三件事一起完成（owner 2026-10-22 的 P1–P3）：
      · P1：狀態引出 → `TurnContext.state.state` / `.state_block`（貼進回覆 prompt）
      · P2：狀態換算成 AEM 的 delta（心情與「讀法」用同一條曲線，不打架）
      · P3：`social_condition`（informational / normative）交給路由層決定要不要接話

    失敗一律靜默：她是來聊天的，不是來跑管線的。
    """

    name = "state.elicit"

    def __init__(self, *, cores=None, affect=None, call=None, log=None,
                 log_path=None, enabled: bool | None = None) -> None:
        self.cores = cores if cores is not None else {}
        self.affect = affect
        self.call = call
        self._log = log or (lambda _message: None)
        self.log_path = pathlib.Path(log_path) if log_path else None
        self.enabled = enabled
        self.elicited = 0
        self.failed = 0
        self.total_ms = 0

    def run(self, env, ctx):
        context = TurnContext.of(env)
        if not should_elicit(group=bool(context.persona.group),
                             source=str(env.event.source or ""),
                             mode=str(context.persona.mode or ""),
                             enabled=self.enabled):
            return None
        core = (self.cores or {}).get(env.decision.entity)
        if not core:
            return None
        if self.call is None:
            self._log("state: 沒有編譯通道 ⇒ 跳過（設 BLUEAGENT_LLM_* 或注入 call）")
            return None
        recent = [str(turn.text)[:160] for turn in (env.thread.transcript or [])][-3:]
        mood = ""
        affect = getattr(self, "affect", None)
        if affect is not None:
            try:
                mood = str(affect.mood(env.decision.entity).get("label") or "")
            except Exception:                           # noqa: BLE001
                mood = ""
        state = elicit_state(core, text=env.event.text,
                             scene="group" if context.persona.group else "trpg",
                             recent=recent, mood=mood, call=self.call,
                             log=self._log)
        if state is None:
            self.failed += 1
            return None
        self.elicited += 1
        self.total_ms += int(state.get("elapsed_ms") or 0)
        context.state.state = state
        context.state.social_condition = str(state.get("social_condition") or "")
        block = state_block_of(state)
        if block:
            context.state.state_block = block
        # P2：狀態 → 心情（同一個 entity 只有一個心情）
        if affect is not None:
            try:
                affect.note_state(env.decision.entity, state,
                                  source_event_id=f"{env.thread.id}:{int(env.event.at * 1000)}")
            except Exception as exc:                    # noqa: BLE001
                self._log(f"state: AEM 換算失敗（{type(exc).__name__}: {exc}）")
        self._record(env, state)
        ctx.mark("state")                     # 打點：狀態引出（SPIN 第②段）做完
        return None

    def _record(self, env, state: dict) -> None:
        """落一筆狀態日誌（校準用：規則判的條件 vs 模型判的條件）。"""
        if self.log_path is None:
            return
        row = {"at": round(float(state.get("at") or time.time()), 3),
               "entity": env.decision.entity, "principal": env.decision.principal,
               "source": str(env.event.source or ""), "state": state}
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def snapshot(self) -> dict:
        return {"elicited": self.elicited, "failed": self.failed,
                "avg_ms": int(self.total_ms / self.elicited) if self.elicited else 0}
