"""她在語音房的**在場決策**：什麼時候自己進去、什麼時候自己退、什麼時候先問一句。

這是 owner 2026-10-22 給的權限：

> 「給她一個主動退出 DC 的 VC 的決策權。比如 VC 裡面沒有人我待一會就退出。
>   主人進 VC 問主人需不需要自己進去。等等。」

所以三條規矩（都是**她的 action**，不是 UI 開關）：

| 情況 | 她的決定 |
|---|---|
| 她在房裡，房裡沒有「該聽的人」持續 `empty_leave_s` | **自己退**（記一筆私記，不出聲、不向主人匯報） |
| 她不在房裡，主人（或白名單內的人）進了她的語音房 | **先問一句**「要我進去嗎？」（同一人 `ask_cooldown_s` 一次；安靜時段不問） |
| 白名單外的人進房 | **什麼都不做**（她本來就不該聽陌生人；他進來也不代表要她陪） |

`autojoin=1` 時第二條改成「直接進」（不問）——這是 owner 可以調的口味，預設是**問**。

刻意分開的兩層：
  · 這個模組只有**政策**（純資料進、純決策出，可離線測）；
  · 「斷線／連線／貼文字」全部在 adapter（`adapters/discord_voice.py`）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

JOIN = "join"
LEAVE = "leave"
ASK = "ask"

#: 主人可以用「一句話」直接命令她進出（確定性規則，不經模型——模型會回「好」但不會真的動）。
_JOIN_WORDS = {
    "进来", "进来吧", "进来呀", "进来啊", "进来说话", "进语音", "来语音", "来语音房",
    "入って", "入ってきて", "入って来て", "来て", "おいで", "きて", "こっち来て",
}
_LEAVE_WORDS = {
    "退吧", "退了吧", "退出", "出去", "退下", "你可以走了", "先退吧",
    "出て", "出てって", "退室", "もういい", "もういいよ",
}


def parse_voice_command(text: str) -> str:
    """主人那句話是不是「進房／退房」的命令。回 `join` / `leave` / `""`。

    刻意**只認短句**（去標點後整句等於表裡某一個）：語音轉寫常有多餘空白，
    但不能讓「我要進去了」這種敘述誤判成命令。
    """
    body = "".join(char for char in str(text or "").strip() if char not in " 　!！?？。.、,，~～")
    if not body or len(body) > 8:
        return ""
    if body in _JOIN_WORDS:
        return JOIN
    if body in _LEAVE_WORDS:
        return LEAVE
    return ""


@dataclass(frozen=True)
class PresenceConfig:
    #: 她該待的語音房（Discord channel id）。
    channel_id: str = ""
    #: 房裡沒人多久 ⇒ 她自己退（秒）。
    empty_leave_s: float = 120.0
    #: 問「要我進去嗎」的冷卻（同一個人，秒）。
    ask_cooldown_s: float = 900.0
    #: 剛進房的前幾秒不判「沒人」（他可能正跟著進來）。
    join_grace_s: float = 15.0
    #: 有人進房就直接進（不問）。預設 False＝先問。
    autojoin: bool = False
    #: 安靜時段（她的時區）不主動問，但可以自己退。
    quiet_start: int = 23
    quiet_end: int = 7

    @classmethod
    def from_env(cls, values) -> "PresenceConfig":
        def number(name: str, default: float) -> float:
            try:
                return float(values.get(name) or default)
            except (TypeError, ValueError):
                return default

        return cls(
            channel_id=str(values.get("VOICE_CHANNEL_ID") or "").strip(),
            empty_leave_s=number("BLUEAGENT_VC_EMPTY_LEAVE_S", 120.0),
            ask_cooldown_s=number("BLUEAGENT_VC_ASK_COOLDOWN_S", 900.0),
            join_grace_s=number("BLUEAGENT_VC_JOIN_GRACE_S", 15.0),
            autojoin=str(values.get("BLUEAGENT_VC_AUTOJOIN") or "0").strip().lower()
            in {"1", "on", "true", "yes"},
            quiet_start=int(number("BLUEAGENT_QUIET_START", 23)),
            quiet_end=int(number("BLUEAGENT_QUIET_END", 7)),
        )


@dataclass
class VCPresence:
    """在場決策的狀態機（只有狀態與規則，不碰網路）。"""

    config: PresenceConfig
    #: 白名單（該聽的人）的 Discord id。
    allowed: set[str] = field(default_factory=set)
    joined: bool = False
    #: 房裡的人（只記「人」，bot 不算；白名單外的人另外記，方便診斷）。
    humans: set[str] = field(default_factory=set)
    others: set[str] = field(default_factory=set)
    empty_since: float | None = None
    joined_at: float | None = None
    asked_at: dict[str, float] = field(default_factory=dict)
    #: 最近的決策（給 health／日誌看）。
    actions: list[dict] = field(default_factory=list)
    leaves: int = 0
    asks: int = 0
    joins: int = 0

    # ── 觀測 ────────────────────────────────────────────────────────────────
    def sync_members(self, member_ids, *, now: float | None = None) -> None:
        """剛進房時對一次名冊（Discord 不會補發「他早就在裡面」的事件）。"""
        now = time.monotonic() if now is None else now
        self.humans = set()
        self.others = set()
        for member in member_ids or ():
            self._note_human(str(member))
        self._touch_empty(now)

    def observe(self, *, user_id, is_bot: bool, before_channel: str,
                after_channel: str, quiet: bool = False,
                now: float | None = None) -> list[dict]:
        """有人進出語音頻道。回這次要做的動作（通常是空）。"""
        now = time.monotonic() if now is None else now
        if is_bot:
            return []
        user = str(user_id)
        watch = str(self.config.channel_id)
        entered = str(after_channel or "") == watch and str(before_channel or "") != watch
        left = str(before_channel or "") == watch and str(after_channel or "") != watch
        if entered:
            self._note_human(user)
        elif left:
            self.humans.discard(user)
            self.others.discard(user)
        else:
            return []
        self._touch_empty(now)
        if left or self.joined or not self._is_allowed(user):
            # 他在房裡、她也在 ⇒ 沒什麼要決定；她不在但進來的是陌生人 ⇒ 不理
            return []
        if self.config.autojoin:
            return [self._record({"action": JOIN, "reason": "human_arrived",
                                  "user_id": user}, now)]
        last = self.asked_at.get(user)
        if last is not None and now - float(last) < max(0.0, self.config.ask_cooldown_s):
            return []
        if quiet:
            # 安靜時段：不主動問；等她被叫或他先說話（他在文字頻道喊她，照樣會收到）
            return []
        self.asked_at[user] = now
        self.asks += 1
        return [self._record({"action": ASK, "reason": "human_arrived",
                              "user_id": user}, now)]

    def note_joined(self, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.joined = True
        self.joined_at = now
        self.joins += 1
        self._touch_empty(now)

    def note_left(self, *, now: float | None = None) -> None:
        self.joined = False
        self.joined_at = None
        self.empty_since = None

    # ── 決策 ────────────────────────────────────────────────────────────────
    def tick(self, *, now: float | None = None) -> list[dict]:
        """每跳一次：只有一條規矩——房裡沒人就待一會，然後自己退。回要做的動作。"""
        now = time.monotonic() if now is None else now
        if not self.joined:
            return []
        self._touch_empty(now)
        if self.empty_since is None:
            return []
        if self.config.empty_leave_s <= 0:
            return []
        grace = max(0.0, self.config.join_grace_s)
        if self.joined_at is not None and now - self.joined_at < grace:
            return []
        if now - self.empty_since < max(0.0, self.config.empty_leave_s):
            return []
        self.leaves += 1
        return [self._record({"action": LEAVE, "reason": "empty_room",
                              "empty_s": round(now - self.empty_since, 1)}, now)]

    # ── 內部 ────────────────────────────────────────────────────────────────
    def _is_allowed(self, user_id: str) -> bool:
        return not self.allowed or user_id in self.allowed

    def _note_human(self, user_id: str) -> None:
        if self._is_allowed(user_id):
            self.humans.add(user_id)
        else:
            self.others.add(user_id)

    def _touch_empty(self, now: float) -> None:
        if self.joined and not self.humans:
            if self.empty_since is None:
                self.empty_since = now
        else:
            self.empty_since = None

    def _record(self, action: dict, now: float) -> dict:
        action = {**action, "at": round(now, 3)}
        self.actions.append(action)
        del self.actions[:-20]
        return action

    def snapshot(self) -> dict:
        return {"joined": self.joined, "humans": sorted(self.humans),
                "others": sorted(self.others),
                "empty_for_s": (round(time.monotonic() - self.empty_since, 1)
                                if self.empty_since is not None else 0),
                "leaves": self.leaves, "asks": self.asks, "joins": self.joins,
                "autojoin": self.config.autojoin,
                "empty_leave_s": self.config.empty_leave_s,
                "last_actions": self.actions[-5:]}


__all__ = ["ASK", "JOIN", "LEAVE", "PresenceConfig", "VCPresence", "parse_voice_command"]
