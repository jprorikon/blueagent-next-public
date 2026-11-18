"""「她可不可以用語音」——由 AEM 決定（owner 2026-10-22 第④條）。

規則（刻意保守）：
  · **高喚醒 ＋ 心情正** ⇒ 允許用語音（她很想說，語氣也衝得出來）；
  · 低潮 ⇒ 不用語音（低落時人話少、聲音也低，硬出聲反而奇怪）；
  · 安靜時段／主人在忙 ⇒ 一律文字；
  · 語音只是「允許」，實際能不能出聲仍由通道自己回答（有沒有人聽、音量設定）。
"""

from __future__ import annotations

#: 允許語音的門檻
VOICE_AROUSAL_MIN = 0.60
VOICE_VALENCE_MIN = 0.10


def voice_allowed(mood: dict | None, *, quiet_hours: bool = False,
                  busy: bool = False) -> bool:
    if busy or quiet_hours:
        return False
    data = mood or {}
    try:
        arousal = float(data.get("arousal") or 0.0)
        valence = float(data.get("valence") or 0.0)
    except (TypeError, ValueError):
        return False
    return arousal >= VOICE_AROUSAL_MIN and valence >= VOICE_VALENCE_MIN


def reason(mood: dict | None, *, quiet_hours: bool = False, busy: bool = False) -> str:
    """給 log 用：為什麼給／不給語音。"""
    data = mood or {}
    try:
        arousal = float(data.get("arousal") or 0.0)
        valence = float(data.get("valence") or 0.0)
    except (TypeError, ValueError):
        return "no_mood"
    if busy:
        return "busy"
    if quiet_hours:
        return "quiet_hours"
    if arousal < VOICE_AROUSAL_MIN:
        return "arousal_low"
    if valence < VOICE_VALENCE_MIN:
        return "valence_low"
    return "ok"


__all__ = ["VOICE_AROUSAL_MIN", "VOICE_VALENCE_MIN", "voice_allowed", "reason"]
