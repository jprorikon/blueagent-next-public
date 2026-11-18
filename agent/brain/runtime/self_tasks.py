"""她的「自己的事」什麼時候做——由 AEM 決定（owner 2026-10-22 第②條）。

不是排固定的 cron，而是**依心情挑時機與時長**：

  · **低潮（valence 低）**：先做「整理私記／回顧」這種低耗的事，短、安靜；
  · **平盤**：正常的自主事務（學習／練習／整理）；
  · **高喚醒（arousal 高、valence 正）**：可以做長一點、需要投入的事（練習、鑽研）；
  · **剛跟主人聊完（idle 很短）**：不要立刻跑去做別的事——她還在那個對話裡；
  · **安靜時段**：只做不打擾人的事（寫私記／整理），不主動找人。

刻意只做「決策」，不做「執行」：執行那側（LifeLoop／心跳）拿這個決策去挑任務，
所以規則改了也不必動排程器。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 這些任務在任何心情下都不打擾人（內部整理型）。
QUIET_KINDS = ("journal", "review", "organize")
#: 需要投入、心情好才做得起來的。
DEEP_KINDS = ("study", "practice", "explore")


@dataclass(frozen=True)
class SelfTaskDecision:
    """一次「要不要去做自己的事」的判斷結果。"""

    start: bool
    kind: str = ""
    minutes: float = 0.0
    reason: str = ""

    def as_dict(self) -> dict:
        return {"start": self.start, "kind": self.kind,
                "minutes": round(self.minutes, 1), "reason": self.reason}


def decide(*, valence: float, arousal: float, idle_s: float,
           quiet_hours: bool = False, busy: bool = False,
           min_idle_s: float = 300.0) -> SelfTaskDecision:
    """回一個決策。`idle_s`＝距離上一次跟人互動多久（秒）。"""
    if busy:
        return SelfTaskDecision(False, reason="busy")
    if idle_s < min_idle_s:
        # 剛聊完不要馬上跑掉：她還在那個對話的餘韻裡
        return SelfTaskDecision(False, reason="just_talked")

    # 低潮：只做安靜、短的內部整理
    if valence <= -0.2:
        return SelfTaskDecision(True, kind="journal", minutes=10.0, reason="low_mood_quiet")

    # 安靜時段：不打擾人，只做內部的事
    if quiet_hours:
        return SelfTaskDecision(True, kind="organize", minutes=15.0, reason="quiet_hours")

    # 心情好、精神也足：做需要投入的
    if valence >= 0.25 and arousal >= 0.45:
        return SelfTaskDecision(True, kind="practice", minutes=40.0, reason="energised")

    # 平盤：正常長度
    if valence >= 0.0:
        return SelfTaskDecision(True, kind="study", minutes=25.0, reason="steady")

    return SelfTaskDecision(True, kind="review", minutes=15.0, reason="slightly_low")


__all__ = ["SelfTaskDecision", "decide", "DEEP_KINDS", "QUIET_KINDS"]
