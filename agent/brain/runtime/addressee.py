"""群聊裡「這句話在對誰說」——收件人狀態機（2026-10-27）。

owner 回報的問題：他在 Discord 群裡跟**名單外的人**、甚至跟另一個 bot（Hermes）一來一回，
她都會插進來。舊判定有兩個漏洞：

  ① `continuation` 只看「她剛在這頻道說完 ＋ 這則沒有 @ 別人」⇒ 只要窗口沒過，
     任何人的下一句都算「在對她說」；
  ② 主人例外（`owner_speaking`）讓「公開表態」也一律算對她說。

現在只認三種「明確對她說」（@ 她／引用回覆她／文字叫她），外加一種**有條件的延續**：
她剛被某個人叫到、**同一個人**在窗口內接著講。別人（人或 bot）一開口就解除延續——
所以她不會在「主人 ↔ 其他人／其他 bot」的對話裡插話。

純規則、零延遲、可測（`tests/test_addressee.py`）。狀態只在記憶體：窗口是分鐘級的東西，
重啟後從「沒有延續」開始是安全的（寧可少接一句，不要搶話）。
"""

from __future__ import annotations

import time

ENTITY = "entity"
UNKNOWN = "unknown"
GROUP = "group"

#: 名字後面接這些才算「在叫人」（句中呼叫：「〜、ミラ？」「〜、ミラさん！」）。
#: ⚠ 不收「は／って／が」這類助詞：「ミラはどう思う？」在句中是**談到**她，不是叫她
#:   （2026-10-27 審查：「ノリ、ミラはどう思う？」以前兩顆 bot 都當成在叫自己）。
_CALL_MARKS = ("、", "，", ",", "。", "！", "!", "？", "?", " ", "　", "…", "〜", "~")
#: 敬稱：後面還要再接呼叫標記或句尾才算（「ミラさんが来た」不算叫她）
_HONORIFICS = ("さん", "ちゃん", "くん")


class ChannelState:
    __slots__ = ("engaged_until", "engaged_speaker", "last_speaker", "last_reason")

    def __init__(self) -> None:
        self.engaged_until = 0.0
        self.engaged_speaker = ""
        self.last_speaker = ""
        self.last_reason = ""


class AddresseeTracker:
    """每個頻道一份狀態；`resolve()` 回 `(收件人, 理由)`，理由進日誌當標定資料。"""

    def __init__(self, *, window_s: float = 180.0) -> None:
        self.window_s = max(0.0, float(window_s))
        self._channels: dict[str, ChannelState] = {}

    def state(self, channel_id: str) -> ChannelState:
        return self._channels.setdefault(str(channel_id or ""), ChannelState())

    def note_other_speaker(self, channel_id: str, author_id: str, *, now: float | None = None) -> None:
        """別人（含 bot）在這條頻道發言 ⇒ 解除延續（「主人 ↔ 他人」的來回不算在跟她講）。"""
        state = self.state(channel_id)
        state.last_speaker = str(author_id or "")
        if state.engaged_speaker and str(author_id or "") != state.engaged_speaker:
            state.engaged_until = 0.0
            state.engaged_speaker = ""
            state.last_reason = "cleared_by_other"

    def engage(self, channel_id: str, author_id: str, *, now: float) -> None:
        state = self.state(channel_id)
        if self.window_s <= 0:
            return
        state.engaged_speaker = str(author_id or "")
        state.engaged_until = float(now) + self.window_s
        state.last_speaker = str(author_id or "")

    def resolve(self, *, channel_id: str, author_id: str, text: str = "",
                mentioned_me: bool = False, mentioned_other_ids=(),
                reply_to_me: bool = False, reply_other_id: str = "",
                my_names=(), other_names=(), roster: dict | None = None,
                now: float | None = None) -> tuple[str, str]:
        """回 `("entity"|"other:<id>"|"group"|"unknown", 理由)`。"""
        moment = float(now if now is not None else time.time())
        author = str(author_id or "")
        body = str(text or "")
        state = self.state(channel_id)
        if state.engaged_speaker and state.engaged_speaker != author:
            # 別人（人也算）開口 ⇒ 延續解除。以前只有 bot 會走 `note_other_speaker`，
            # 名單外的真人插一句不會清 ⇒ 主人回那個人時還算「在跟她講」（2026-10-27 審查）。
            self._clear(state, "cleared_by_other")
        state.last_speaker = author
        if mentioned_me:
            self.engage(channel_id, author, now=moment)
            return ENTITY, "mention"
        if reply_to_me:
            self.engage(channel_id, author, now=moment)
            return ENTITY, "reply_to_me"
        # 文字叫名字：**誰在前面誰算**。「ノリ、ミラはどう思う？」是在問ノリ，
        # 不能因為先查她自己的名字就當成叫她。
        mine_at, _label = _call_at(body, my_names)
        other_at, other_label = _call_at(body, other_names)
        roster_at, roster_id = _call_at_roster(body, roster or {}, my_names)
        rivals = [pos for pos in (other_at, roster_at) if pos >= 0]
        if mine_at >= 0 and (not rivals or mine_at <= min(rivals)):
            self.engage(channel_id, author, now=moment)
            return ENTITY, "named"
        others = [str(item) for item in (mentioned_other_ids or ()) if str(item)]
        if others:
            self._clear(state, "mention_other")
            return f"other:{others[0]}", "mention_other"
        if reply_other_id:
            self._clear(state, "reply_other")
            return f"other:{str(reply_other_id)}", "reply_other"
        # 叫的是**別的角色**（這顆 bot 的喚醒詞表裡也有 aster／nori 的名字）⇒ 不是叫她。
        # 一顆 bot 只是一個實體（`entity_for()` 永遠回 default_entity），
        # 所以「ノリ、これ」絕不能讓 ミラ 回。
        if other_at >= 0 and (roster_at < 0 or other_at <= roster_at):
            self._clear(state, "named_other_character")
            return f"other:{other_label}", "named_other_character"
        if roster_at >= 0:
            self._clear(state, "named_other")
            return f"other:{roster_id}", "named_other"
        if state.engaged_speaker == author and state.engaged_until > moment:
            self.engage(channel_id, author, now=moment)      # 滑動窗口：同一個人接著講
            return ENTITY, "engaged"
        state.last_reason = "no_signal"
        return UNKNOWN, "no_signal"

    @staticmethod
    def _clear(state: ChannelState, reason: str) -> None:
        state.engaged_until = 0.0
        state.engaged_speaker = ""
        state.last_reason = reason


def _named(body: str, names) -> str:
    """文字裡有沒有叫到某個名字；有就回那個名字（最前面那一個）。"""
    return _call_at(body, names)[1]


def _call_at(body: str, names) -> tuple[int, str]:
    """最早的一次「呼叫」在哪：回 `(位置, 名字)`；沒有就 `(-1, "")`。

    只認**句首**或**後面緊跟呼叫標記**的名字，避免「話題剛好提到某人」被當成在叫他。
    位置用來比「先叫誰」：句首（0）一定贏過句中。
    """
    if not body:
        return -1, ""
    head = body.lstrip("＠@ 　")
    offset = len(body) - len(head)
    best = (-1, "")
    for name in names:
        label = str(name or "").strip()
        if not label or len(label) < 2:
            continue
        if head.startswith(label):
            pos = offset
        else:
            pos = -1
            start = body.find(label)
            while start >= 0:
                if _is_call_tail(body[start + len(label):]):
                    pos = start
                    break
                start = body.find(label, start + 1)
        if pos >= 0 and (best[0] < 0 or pos < best[0]):
            best = (pos, label)
    return best


def _is_call_tail(rest: str) -> bool:
    for honorific in _HONORIFICS:
        if rest.startswith(honorific):
            rest = rest[len(honorific):]
            break
    return not rest or rest.startswith(_CALL_MARKS)


def _call_at_roster(body: str, roster: dict, my_names) -> tuple[int, str]:
    """最早叫到的**別人**（roster: id → 顯示名）：回 `(位置, id)`。"""
    mine = {str(name).strip() for name in (my_names or ()) if str(name).strip()}
    best = (-1, "")
    for other_id, display in roster.items():
        label = str(display or "").strip()
        if not label or label in mine:
            continue
        pos, _label = _call_at(body, (label,))
        if pos >= 0 and (best[0] < 0 or pos < best[0]):
            best = (pos, str(other_id))
    return best


__all__ = ["AddresseeTracker", "ChannelState", "ENTITY", "GROUP", "UNKNOWN"]
