"""`remind.in`：她替自己排「幾跳之後／幾分鐘後／幾點」要提某件事（2026-10-27）。

為什麼是「跳」：心跳是她自己的節拍（30–75 秒一跳，arousal 會調），比秒更像她自己的時間感；
owner 要的正是「她知道幾跳之後要提某件事」。

邊界：
  · 只排**自己的**（`ctx.entity`），別人的預約看不到也改不到；
  · 到期由心跳取出並標記 fired（只講一次）；講失敗也算用掉，避免每跳重複；
  · 上限 30 筆，多的排不進去（避免她把提醒堆成山）。
"""

from __future__ import annotations

import time

from agent.brain.runtime.tools.base import ToolContext, ToolResult

MAX_PENDING = 30


class RemindTool:
    name = "remind.in"

    schema = {
        "type": "function",
        "function": {
            "name": "remind_in",
            "description": (
                "自分のために「あとで話題に出す」ことを予約する。相手に「また後で聞くね」「あとで知らせる」と約束するときに使う。"
                "何拍あと（after_ticks、鼓動 1 拍はおよそ 30〜75 秒）、何分あと（after_minutes）"
                "または時刻（at、24 時間制 HH:MM）で指定できる。note には話題に出すことを一言で。"
                "時間が来たら鼓動がこの一言を渡してくれるので、どう切り出すかは自分で決める。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "話題に出すこと（一言）"},
                    "after_ticks": {"type": "integer", "description": "何拍あと（例：20）"},
                    "after_minutes": {"type": "integer", "description": "何分あと（例：90）"},
                    "at": {"type": "string", "description": "時刻（HH:MM、24 時間制）"},
                },
                "required": ["note"],
            },
        },
    }

    def __init__(self, store, beats=None) -> None:
        self.store = store
        #: `beats(entity) -> {"ticks": int, "interval_s": float}`；沒有就當 0
        self.beats = beats

    def _beat_state(self, entity: str) -> dict:
        if self.beats is None:
            return {}
        try:
            return dict(self.beats(str(entity)) or {})
        except Exception:                              # noqa: BLE001 - 排程不該擋回話
            return {}

    def run(self, ctx: ToolContext, arguments: dict) -> ToolResult:
        entity = str(getattr(ctx, "entity", "") or "")
        note = str((arguments or {}).get("note") or "").strip()
        if not entity:
            return ToolResult(ok=False, error="no_entity")
        if not note:
            return ToolResult(ok=False, error="note required")
        pending = self.store.pending(entity, tick=0) if hasattr(self.store, "pending") else []
        if len(pending) >= MAX_PENDING:
            return ToolResult(ok=False, error=f"too_many_reminders（もう {len(pending)} 件予約してある）")
        beat = self._beat_state(entity)
        try:
            record = self.store.add(
                entity, note,
                tick=int(beat.get("ticks") or 0),
                after_ticks=(arguments or {}).get("after_ticks"),
                after_minutes=(arguments or {}).get("after_minutes"),
                at_time=str((arguments or {}).get("at") or ""),
                interval_s=float(beat.get("interval_s") or 0.0))
        except (TypeError, ValueError) as exc:
            return ToolResult(ok=False, error=f"bad_arguments:{exc}")
        when = _when(record, now_tick=int(beat.get("ticks") or 0))
        return ToolResult(ok=True, data={
            "note": note, "at_tick": record.get("at_tick") or 0,
            "at": record.get("at") or 0.0, "when": when,
            "summary": f"覚えた：{when}に「{note}」を話題に出す。"})


def _when(record: dict, *, now_tick: int) -> str:
    """給她看的「什麼時候」：**相對拍數**＋時刻（`HH:MM`）。

    `at_tick` 是絕對跳號；以前直接寫成「{at_tick} 跳後」⇒ 第 500 跳排「20 跳後」顯示成
    「520 跳後」，時刻那條則是一串 unix 秒（2026-10-27 審查）。
    """
    parts = []
    at_tick = int(record.get("at_tick") or 0)
    if at_tick:
        parts.append(f"{max(0, at_tick - int(now_tick))} 拍あと（{at_tick} 拍目）")
    elif int(record.get("est_tick") or 0):
        # 分鐘預約：跳數只是估計（到期看時刻）
        parts.append(f"およそ {max(0, int(record['est_tick']) - int(now_tick))} 拍あと")
    stamp = float(record.get("at") or 0.0)
    if stamp:
        moment = time.localtime(stamp)
        day = "" if moment[:3] == time.localtime()[:3] else f"{moment.tm_mon}月{moment.tm_mday}日 "
        parts.append(day + time.strftime("%H:%M", moment))
    return "／".join(parts) or "そのうち"


__all__ = ["MAX_PENDING", "RemindTool"]
