"""情感整理的接線（docs/PLAN-affect-reorg-20261109.md §3-6／§3-7／§3-8）。

App adapter 建好之後由 `brain/app/state.py` 呼叫 `wire(app, adapters)`：

  · **公共區反饋進心情**（B.App.3）：赞 ⇒ pleasure +0.05/+0.02；回帖 ⇒ interest +0.08/+0.10；
    對象記成 `board:<帖>`（主人看不見的原因）。節流沿用 `BoardEventBridge`（同帖同人同動作 5 分鐘、每天 40）
    ＋ `dampen`（同種同幅連續 3 次後折半）。原本的世界事件照發，這裡只是多接一條。
  · **被 `@` 時拿起率乘心情係數**（GROUP-SELF-CHAT §12-3）：`mood_words.called_factor`。

`ignored`（已讀 3 小時沒回）不在這裡：它跟著心跳走（`Heartbeat._note_ignored`）。
"""

from __future__ import annotations

import time

from agent.brain.runtime.inner.mood_words import called_factor

#: 公共區反饋 → (episode kind, Δv, Δa)
BOARD_AFFECT = {"board.like": ("pleasure", 0.05, 0.02), "board.reply": ("interest", 0.08, 0.10)}


def board_to_affect(affect, entity: str, notice: dict) -> dict:
    """一條公共區回流 ⇒ 一筆小小的 appraisal（她自己知道、主人不一定知道的開心）。"""
    row = BOARD_AFFECT.get(str((notice or {}).get("kind") or ""))
    if affect is None or row is None:
        return {"ok": False}
    kind, delta_v, delta_a = row
    post = str(notice.get("post_id") or "")
    # 她自己這邊的事 ⇒ 核心的 `outcome`（不掛在「有人對她說話」上，不重設和主人的閒置計時）
    return affect.outcome(
        str(entity), kind=kind, delta_v=delta_v, delta_a=delta_a, confidence=0.5,
        event_id=f"board:{notice.get('kind')}:{post}:{notice.get('actor_id') or ''}:{int(time.time() // 300)}",
        target_type="conversation", target_id=f"board:{post}")


def wire(app, adapters) -> None:
    affect = getattr(app, "affect", None)
    if affect is None:
        return
    for adapter in adapters or ():
        attention = getattr(adapter, "group_attention", None)
        if attention is not None:
            attention.mood = lambda entity, at, _affect=affect: called_factor(
                str((_affect.mood(str(entity), at=at) or {}).get("label") or ""))
        original = getattr(adapter, "on_board", None)
        if original is None or getattr(original, "_affect_wired", False):
            continue

        def on_board(entity, kind, notice, event, _original=original):
            _original(entity, kind, notice, event)
            try:
                board_to_affect(affect, entity, notice)
            except Exception:                              # noqa: BLE001 - 心情壞了不擋回流
                pass

        on_board._affect_wired = True
        adapter.on_board = on_board


__all__ = ["BOARD_AFFECT", "board_to_affect", "wire"]
