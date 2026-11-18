"""一轮对话 → 该沉成哪些事实。**纯规则**，不碰存储。

为什么单独一个档：这条规则要同时被两个后端用（内存 gram 版 / SQLite 索引版）。
复制两份迟早会漂（改了一边忘了另一边），所以抽出来当唯一真相。

规则本身与最初的记忆体版逐字相同，搬出来时不改行为：
  · 无主源（她自己）⇒ 每轮一条 `self` 经历
  · 明说「记住/记一下」⇒ 一条 `explicit`
  · 「我(喜欢|讨厌|…)X」⇒ 一条 `rule`
  · 「叫我X」⇒ 一条称谓
  · 「/ 说好 / 大家都」⇒ 一条 `consensus`
  · 说「秘密／别告诉」⇒ 压回 `private`，只留说话的人
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping

from agent.brain.memory.facts import ADDRESS_SLOT, Fact, game_of
from agent.brain.domain.thread import MemoryScope, TurnRecord

SECRET_WORDS = ("秘密", "別告訴", "别告诉", "不要告诉", "不要告訴")
REMEMBER_WORDS = ("记住", "記住", "记一下")
CONSENSUS_WORDS = ("大家都", "我们说好", "我們說好", "一起约定",
                   "一起約定", "说好了", "說好了")
_LIKE = re.compile(r"我(喜欢|討厭|讨厌|害怕|住在|养了|養了|擅长|擅長|不擅长|不擅長)"
                   r"([^，。！？,.!?]{1,26})")
_CALLED = re.compile(r"叫我([^，。！？,.!?\s]{1,12})")


def turn_scope(scope: MemoryScope, participants: Iterable[str]) -> set[str]:
    """這一輪「在場的人」＝發話者 ＋ 送進來的名冊。"""
    people = {str(person).strip() for person in participants if str(person).strip()}
    if scope.principal:
        people.add(scope.principal)
    return people


def facts_for_turn(scope: MemoryScope, turn: TurnRecord, *,
                   participants: Iterable[str] = (),
                   names: Mapping[str, str] | None = None) -> list[Fact]:
    """回這一輪要落的事實（可能空）。不做去重、不寫盤——那是後端的事。"""
    if turn.role != "user" or not str(turn.text or "").strip():
        return []
    names = names or {}
    text = turn.text.strip()
    people = turn_scope(scope, participants)
    # 房間成員＝發話者＋名冊＋**她自己**（owner 2026-10-23：層數要含她在內算，
    # 所以「她 + 1 人」是 pair、「她 + 2 人以上」才是 circle；audience 也一併記她）。
    room = frozenset(people | {scope.entity}) if scope.entity else frozenset(people)
    self_owned = bool(scope.self_owned)
    who = "她" if self_owned else names.get(scope.principal, scope.principal or "他")
    audience = room
    alone = len(room) <= 1
    pair = len(room) == 2
    selfish = self_owned or alone
    layer = "self" if selfish else ("pair" if pair else "circle")
    label = "她的经历" if selfish else ("两人" if pair else "在场的人")

    if any(word in text for word in SECRET_WORDS):
        # 秘密：知情者只有「告訴她的那個人」＋她本人 ⇒ 兩個人都到了才可說出。
        audience, layer, label = frozenset(
            {scope.principal, scope.entity} - {""}), "private", "私密"

    facts: list[Fact] = []
    if self_owned:
        facts.append(Fact(text=f"她在「{scope.scene}」：{text[:80]}", layer="self",
                          audience=audience, label="她的经历", self_owned=True,
                          source="self"))
    if any(word in text for word in REMEMBER_WORDS):
        facts.append(Fact(text=f"{who}说过：{text[:60]}", layer=layer,
                          audience=audience, label=label, principal=scope.principal,
                          self_owned=self_owned, source="explicit"))
    for match in _LIKE.finditer(text):
        verb, obj = match.group(1), match.group(2).strip()
        if obj:
            facts.append(Fact(text=f"{who} {verb}{obj}", layer=layer,
                              audience=audience, label=label, principal=scope.principal,
                              self_owned=self_owned, source="rule"))
    called = _CALLED.search(text)
    if called:
        facts.append(Fact(text=f"{who}希望被叫「{called.group(1)}」", layer=layer,
                          audience=audience, label=label, principal=scope.principal,
                          self_owned=self_owned, source="rule"))
    if any(marker in text for marker in CONSENSUS_WORDS):
        facts.append(Fact(text=text[:80], layer="consensus", audience=audience,
                          label="共识", principal=scope.principal, consensus=True,
                          self_owned=False, source="explicit"))
    # 分倉鍵：這一輪的事實全部屬於「當前這個角色實體」的倉（owner 2026-10-23 按實體分倉）
    entity_id = str(scope.entity or "")
    # 場景維度（B3）：桌（TRPG）上的一輪抽出來的事實全部是遊戲裡的事 ⇒ 在**這裡**打標，
    # 兩個後端只負責照抄（2026-11-15 審核：以前只有蒸餾打標，這條即時路徑寫成了現實記憶）。
    game = game_of(turn.source)
    for fact in facts:
        fact.entity = entity_id
        fact.game = game
    return facts


# ── 稱呼：唯一權威寫法（owner 2026-11-03「收斂到 オーナー」）──────────────────────
#: 為什麼要有它：庫裡累積了 190 條「她怎麼叫主人」的事實，兩種版本（船長／オーナー）
#: **都算現行有效**，撈到哪條她就叫哪個——這是她偶爾叫錯稱呼的根因，不是 prompt 的問題。
#: 收斂做法：任何「稱呼類」的事實，寫進來之前一律改成這一句 ⇒ 同一句＝同一 norm
#: ⇒ `add()` 本來就會擋重複，不會再堆第二條；再把槽位綁死，新的自動收掉舊的。
ADDRESS_CANON = "主人のことは「オーナー」と呼ぶ。"
#: 稱呼事實的狀態槽位：`ADDRESS_SLOT`（定義在 `facts.py`，與 `single_valued` 放在一起；這裡照舊轉出）
#: 稱呼相關的詞（人稱）
_NAMES = r"(?:船長|船长|オーナー|オーナー)"
_VERBS = r"(?:呼|叫|稱|称)"
#: 稱呼句的形狀：人稱詞與稱呼動詞**貼在一起**（≤6 字），不是「句子裡剛好兩個詞都出現」。
#: 實測（2026-11-03）：只要求「同時出現」⇒ 長篇活動筆記也被判成稱呼事實（aster 誤判 3 條）。
_ADDRESS = re.compile(_NAMES + r"[」』\"'）)]?.{0,6}?" + _VERBS
                      + r"|" + _VERBS + r".{0,8}?" + _NAMES)
#: 稱呼事實都很短；長句子一律不碰（避免把日誌改寫掉）
_ADDRESS_MAX_CHARS = 100


def is_address(text: str) -> bool:
    """這條是不是在講「她怎麼稱呼主人」。

    判據：**人稱詞與稱呼動詞在 6–8 字內相鄰**，且整句不長。
    只提到「オーナー」但沒有稱呼動詞的（「ゲストの依頼はオーナーに回す」）、或是長篇筆記裡
    剛好兩個詞都出現的，都不算——那種句子改寫會失真。
    """
    body = str(text or "").strip()
    if not body or len(body) > _ADDRESS_MAX_CHARS:
        return False
    return bool(_ADDRESS.search(body))


def canonical_address(text: str) -> str:
    """稱呼類事實 → 權威寫法；不是稱呼類就原樣回傳。"""
    return ADDRESS_CANON if is_address(text) else str(text or "")


__all__ = ["ADDRESS_CANON", "ADDRESS_SLOT", "CONSENSUS_WORDS", "REMEMBER_WORDS",
           "SECRET_WORDS", "canonical_address", "facts_for_turn", "is_address",
           "turn_scope"]
