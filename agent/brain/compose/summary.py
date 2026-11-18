"""片段摘要（逐句退路）：沒有提煉窗口摘要時，用前幾句拼一段承接。"""

from __future__ import annotations

from typing import Mapping

from agent.brain.domain.episode import Episode


def summarize(episode: Episode, names: Mapping[str, str] | None = None) -> str:
    """`names`＝principal id → 顯示名（傳 `PrincipalRegistry.names()`）；沒有就用 id。"""
    who_is = names if names is not None else {}
    lines: list[str] = []
    for turn in episode.turns:
        who = who_is.get(turn.speaker, turn.speaker) or (
            "彼女" if turn.role == "assistant" else "相手")   # 日語：這串進 carry_summary／[[before]]
        text = turn.text.strip().replace("\n", " ")
        if text:
            lines.append(f"{who}：{text[:26]}")
    return "；".join(lines[:4])[:180]
