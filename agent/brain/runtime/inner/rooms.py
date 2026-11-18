"""房間：**世界變化與分層記憶共用的那一個鍵**（owner 2026-11-03「語義對齊」）。

兩邊說的其實是同一件事，差別只在驅動時機：

    回合驅動 → 沉澱成記憶（寫庫、有 `valid_to`、按 audience 分層）
    心跳／步進驅動 → 世界變化（一次性視圖、不落庫、同一套房間鍵）

所以房間的**表示**只能有一份：這裡。判定也只有一份（`runtime/memory/disclosure.py` 的
`disclosable`，在場 ⊆ 房間）。以前各寫一套，遲早一邊放寬、一邊沒放寬。

四個 label：

    world          沒有房間（誰都能讀）
    self           只有她自己（audience={她}）
    pair(owner)    她和某一個人（一對一）
    circle(n)      她和一群人（群聊／桌）
"""

from __future__ import annotations

#: label → 人話（世界變化那一行用；不進判定）
_LABEL_TEXT = {"world": "世界", "self": "自分"}


def audience_of(entity: str, others=()) -> set[str]:
    """把「她 ＋ 幾個對象」組成房間（audience 就是這個集合）。"""
    room = {str(entity)} if str(entity or "").strip() else set()
    room |= {str(who) for who in (others or ()) if str(who or "").strip()}
    return room


def label_of(audience, *, entity: str = "") -> str:
    """`audience`（房間）→ 四類 label 之一。空房間是 `world`，只有她是 `self`。"""
    room = {str(who) for who in (audience or ()) if str(who or "").strip()}
    if not room:
        return "world"
    others = room - {str(entity)} if str(entity or "").strip() else set(room)
    if not others:
        return "self"
    if len(others) == 1:
        return f"pair({sorted(others)[0]})"
    return f"circle({len(others)})"


def room_of_thread(thread, *, entity: str = "") -> set[str]:
    """一條線的房間＝參與過的人 ＋ 她（她自己永遠在房間裡）。"""
    people = {str(who) for who in (getattr(thread, "participants", ()) or ())
              if str(who or "").strip()}
    if str(entity or "").strip():
        people.add(str(entity))
    return people


def self_room(entity: str, *, scope: str = "") -> set[str]:
    """她自己那層事實（日記、活動成果）的房間：**只有她自己**。

    owner 2026-11-03 定的兩個域：

        「她和主人」＝ pair(owner)     —— 兩個人共同的域
        「僅她自己」  ＝ self          —— **她持有真正的秘密**

    這兩個域不能合併：合併之後她的私記就變成「主人當然看得到」，
    而她要不要把私事告訴主人，是**她自己的決定**（不是許可權預設給的）。

    `scope="pair_owner"` 只在特殊情況用（例如某類事實本來就是兩人共有的），默認不開。
    """
    room = {str(entity)} if str(entity or "").strip() else set()
    if str(scope or "").strip().lower() in {"pair_owner", "pair", "with_owner"}:
        return room | {"owner"}
    return room


def human_label(label: str, *, entity: str = "") -> str:
    """label → 進 prompt 的一句人話（日語；比對用的中文不放進來）。"""
    text = str(label or "")
    if text == "world":
        return "世界"
    if text == "self":
        return "自分だけ"
    if text.startswith("pair("):
        who = text[5:-1]
        return "個別（主人）" if who == "owner" else "個別"
    if text.startswith("circle("):
        count = text[7:-1]
        return f"{count}人の場"
    return ""


__all__ = ["audience_of", "label_of", "room_of_thread", "human_label"]
