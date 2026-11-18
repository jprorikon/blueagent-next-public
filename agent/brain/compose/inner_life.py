"""她腦內的背景活動：子代理（回合內）、閒時預想、每日日記、記憶效用記帳。

從 `bootstrap.build()` 拆出來（2026-10-25 結構 review 第 3 階段）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _log(message: str) -> None:
    print(f"  {message}", flush=True)


@dataclass
class InnerLife:
    orchestrator: object
    prethinker: object
    reflection: object


def build_inner_life(base, *, memory, memory_search, raw_log, activity_log, principals,
                     entity_ids, side) -> InnerLife:
    """子代理（查記憶＋內心話）、提前想（閒時背景）、每日日記（一天一次）。"""
    from agent.brain.runtime.prethink import PreThinker, start_worker as start_prethink
    from agent.brain.runtime.reflection import DailyReflection, start_worker as start_reflection
    from agent.brain.runtime.subagents import InnerVoice, RecallScout, SubAgentOrchestrator
    sub_call = side.sub if side.sub_enabled else None
    # 閒時預想（提前想）：背景跑，不佔回話時間；下一輪由 SubAgentStage 帶進 prompt
    prethinker = PreThinker(raw_log=raw_log, call=sub_call,
                            root=base / "prethink", entities=entity_ids,
                            idle_s=float(os.environ.get("BLUEAGENT_PRETHINK_IDLE_S") or 150),
                            interval_s=float(os.environ.get("BLUEAGENT_PRETHINK_INTERVAL_S") or 120),
                            max_age_s=float(os.environ.get("BLUEAGENT_PRETHINK_MAX_AGE_S") or 1800),
                            activity_log=activity_log)
    if str(os.environ.get("BLUEAGENT_PRETHINK") or "1").strip().lower() \
            not in {"0", "false", "no", "off"}:
        start_prethink(prethinker, tick_s=float(os.environ.get("BLUEAGENT_PRETHINK_TICK_S") or 60))
    # 每日自我總結（owner 2026-10-24 第 C 件）：一天一次，三行日記 → notes/ 與記憶
    reflection = DailyReflection(root=base / "notes", memory=memory, activity_log=activity_log,
                                 raw_log=raw_log, call=side.sub, entities=entity_ids,
                                 host=principals.default(), log=_log)
    if str(os.environ.get("BLUEAGENT_REFLECTION") or "1").strip().lower() \
            not in {"0", "false", "no", "off"}:
        start_reflection(reflection,
                         tick_s=float(os.environ.get("BLUEAGENT_REFLECTION_TICK_S") or 3600))
    orchestrator = SubAgentOrchestrator(
        [RecallScout(memory_search), InnerVoice(sub_call)],
        timeout_s=float(os.environ.get("BLUEAGENT_SUBAGENT_TIMEOUT_S") or 6.0),
        # 內心戲只在她「真的需要想」時跑（問句／長句／記憶裡有東西）；`=0` 回到舊行為。
        inner_rule=str(os.environ.get("BLUEAGENT_SUBAGENT_INNER_RULE") or "1").strip().lower()
                   not in {"0", "off", "false", "no"},
        enabled=side.sub_enabled)
    return InnerLife(orchestrator=orchestrator, prethinker=prethinker, reflection=reflection)


def make_usage_recorder(memory):
    """記憶效用記帳：這一輪撈出來的事實有沒有真的用進回覆（owner 2026-10-24）。"""

    def record(*, used, missed) -> None:
        target = getattr(memory, "local", memory)
        note = getattr(target, "note_usage", None)
        if note is None or (not used and not missed):
            return
        if used:
            note(used, used=True)
        if missed:
            note(missed, used=False)

    return record


__all__ = ["InnerLife", "build_inner_life", "make_usage_recorder"]
