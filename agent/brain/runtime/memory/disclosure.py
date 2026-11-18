"""披露層的唯一規則：`在場 ⊆ 房間`（可說）；以及「她自己那層永遠想得起來」（可讀）。

檢索（`memory_search`）與回原文（`memory_open`）都用這一條——兩邊各寫一份，
遲早會一邊放寬、一邊沒放寬。
"""

from __future__ import annotations


def disclosable(present, room) -> bool:
    """`present` 全都在 `room` 裡才給。房間不明（舊資料／找不到原文）⇒ 不說（fail-closed）。"""
    if not room:
        return False
    return set(present) <= {str(who) for who in room}


def readable(fact, present) -> bool:
    """**她這一輪的上下文**收不收這一條。

    owner 2026-11-03「線路 A」：她自己那層（`layer="self"`，日記／活動成果）是**她的域**，
    她永遠想得起來——她就是本體，不需要許可；要不要在這場說出來、對誰說，是**她的決定**
    （靠 prompt 的〔私記〕標記與她自己的判斷，不再用權限表擋）。

    其餘層照舊走披露閘（在場 ⊆ 房間）。`fact` 沒有 `readable()` 時退回同一條規則。
    """
    reader = getattr(fact, "readable", None)
    if reader is not None:
        try:
            return bool(reader(present))
        except Exception:                               # noqa: BLE001 - 判定壞了就當不能說
            return False
    # 跟 `Fact.readable／only_hers` 同一條：只有房間裡只有她自己的 self 才繞過閘（帶別人的日記照舊要過閘）
    audience = getattr(fact, "audience", None)
    me = str(getattr(fact, "entity", "") or "").strip()
    if str(getattr(fact, "layer", "") or "") == "self" and audience is not None:
        members = {str(who) for who in audience}
        if (members <= {me}) if me else len(members) == 1:
            # 同 `Fact.readable` 的 A.13 例外：客人在場、又講到主人 ⇒ 不收
            from agent.brain.memory.facts import OWNER_MENTION, guest_present
            text = str(getattr(fact, "text", "") or "")
            return not (guest_present(present, me) and OWNER_MENTION.search(text))
    return bool(getattr(fact, "disclosable", lambda _present: False)(present))


__all__ = ["disclosable", "readable"]

