"""記憶的基本型：分層、可見域、匹配用的 gram、取代關係的門牌（哈希指針）。"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field, replace
from typing import Iterable

#: 門牌長度（`sha1(normalize_text)` 的前 16 個十六進位字元）
KEY_CHARS = 16

# 中（漢字）＋日（ひらがな・カタカナ・長音・々）＋英數都要留：她講日文，
# 舊版把假名整段濾掉 ⇒「ラノベ」「ほうじ茶」「オーナー」在字面層永遠對不上。
_KEEP = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\u30fc\u3005A-Za-z0-9]")
LAYER_ORDER = {"world": 0, "consensus": 1, "circle": 2, "self": 3,
               "pair": 4, "private": 5}
LAYER_LABEL = {"world": "世界", "consensus": "共识", "circle": "圈子",
               "self": "自己", "pair": "两人", "private": "私密"}

# ── 流（owner 2026-11-03）：記憶的**來源軸**，不是儲存技術 ────────────────────────
#: 為什麼要分：她的生活流水和「他跟她的對話」共用同一個檢索池，600 條窗口只覆蓋 ~2.5 天
#: ⇒ 對話事實被擠到看不見（實測：池子裡對話類 18/8/8 條，全庫 460/15/9）。
#: `dialogue`＝他跟她真的說過的話（越久越值錢）；`life`＝她自己的事（短命，靠主題摘要承接）。
STREAM_DIALOGUE = "dialogue"
STREAM_LIFE = "life"
#: 生活流的來源（她自己的活動與獨白）
LIFE_SOURCES = frozenset({"self_note", "activity_public", "activity_summary",
                          "reflection", "self", "activity"})


#: 生活流單條的壽命（天）：她自己的筆記過了這麼久就退出當前記憶（as-of 還查得到，主題摘要承接）。
#: owner 2026-11-16：7 → 14 天。成果（`activity_summary`）與日記（`reflection`）本來就是 14 天。
LIFE_TTL_DAYS = float(os.environ.get("BLUEAGENT_LIFE_TTL_DAYS") or 14.0)

# ── 客人在場時私記降權（owner 2026-11-16 拍板 A.13／D4 ①）────────────────────────
#: 主人那一側的 principal（腦端 `owner`、App 帳號 `acct_owner`）
HOSTS = ("owner", "acct_owner")
#: 角色實體（不是客人）。開機時 `SqliteMemory` 會用設定裡的 `entity_ids` 補上
_CHARACTERS: set[str] = {"mira", "aster", "nori"}
#: 「這條私記講到主人」：稱呼與指代（與 `fact_rules._NAMES` 同一組稱呼，外加「主人」本身）
OWNER_MENTION = re.compile(r"主人|ご主人|オーナー|オーナー|船長|船长|owner", re.IGNORECASE)


def register_characters(ids: Iterable[str]) -> None:
    """把設定裡的角色實體登記成「不是客人」（多次呼叫只會補，不會減）。"""
    _CHARACTERS.update(str(item).strip() for item in ids or () if str(item).strip())


def guest_present(participants: Iterable[str], me: str = "") -> bool:
    """在場有沒有**主人與角色以外的人**（客人；認不出來的 id 也算——寧可多收一點）。"""
    own = str(me or "").strip()
    for person in participants or ():
        who = str(person or "").strip()
        if who and who != own and who not in HOSTS and who not in _CHARACTERS:
            return True
    return False



def stream_for_source(source: str) -> str:
    """來源 → 流。**不確定的一律算 dialogue**（寧可多記對話，不要漏）。"""
    return STREAM_LIFE if str(source or "").strip() in LIFE_SOURCES else STREAM_DIALOGUE


def game_of(source: str = "", purpose: str = "") -> int:
    """這一輪／這一行屬於哪個場景：`1`＝TRPG 卓上（虛構）、`0`＝現實。**唯一判據**。

    寫入（回合內規則抽取、蒸餾）與讀取（被動召回、`memory.search`／`memory.open`）都用這一支
    ——各寫各的就會漂（2026-11-15 審核：寫入只有蒸餾打標、工具檢索完全沒看場景）。
    `purpose=trpg`（B1 之後 raw 有）或 `source=trpg:*` 任一成立就算桌上。
    """
    if str(purpose or "").strip() == "trpg":
        return 1
    return 1 if str(source or "").strip().lower().startswith("trpg") else 0


#: 稱呼事實的狀態槽位（同一槽位只留一個現值）；`fact_rules.py` 轉出同一個名字
ADDRESS_SLOT = "address"


def single_valued(kind: str, slot: str) -> bool:
    """這條事實佔的是不是「**單值槽位**」（同槽、同場景只留一條現值）。

    owner 2026-11-16：同一槽位保持一條，按時間關係刷新。只有「狀態」與稱呼算單值——
    好惡／約定是多值的（喜歡薄荷**也**喜歡小說），誤帶 slot 也不能互相收掉。
    """
    slot = str(slot or "").strip()
    return bool(slot) and (str(kind or "").strip().lower() == "state" or slot == ADDRESS_SLOT)


def grams(text: str) -> set[str]:
    clean = "".join(_KEEP.findall(text or ""))
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i:i + 2] for i in range(len(clean) - 1)}


def normalize_text(text: str) -> str:
    return "".join(_KEEP.findall(text or ""))


def fact_key(text: str) -> str:
    """一條事實的**門牌**（GLOSSARY.md「門牌」）：正規化後文字的 sha1 前 16 位。

    為什麼用哈希而不是原文當指針（owner 2026-11-01）：
      · 原文會被抽取層改寫（Hindsight 會把句子沉短）⇒ 拿原文當鍵，跨倉對不上；
      · 日誌、`superseded_by`、`supersedes` 裡一串原文沒法看、長度還會爆；
      · 同一段文字在任何倉、任何時間算出來都一樣 ⇒ 兩側各存一個指針就能互指。
    算不出來（全是不留字元）⇒ 空字串，呼叫端必須當成「沒有指針」處理。
    """
    norm = normalize_text(text)
    if not norm:
        return ""
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:KEY_CHARS]


def parse_keys(value) -> tuple[str, ...]:
    """`supersedes` 欄位（逗號分隔）→ 門牌元組；空值與空字串都回空。"""
    if not value:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        parts = [str(item) for item in value]
    else:
        parts = str(value).split(",")
    return tuple(part.strip() for part in parts if part and part.strip())


def join_keys(keys: Iterable[str]) -> str:
    """門牌元組 → 欄位值（去重、排序，讓同一組指針寫出來永遠一樣）。"""
    return ",".join(sorted({str(key).strip() for key in (keys or ()) if str(key).strip()}))



@dataclass
class Fact:
    text: str
    layer: str = "private"
    audience: frozenset[str] | None = None       # None = world
    label: str = ""
    principal: str = ""
    #: 記憶屬於哪個角色實體（`mira` / `aster` / `nori`）——**分倉的鍵**。
    #: 空字串＝舊資料（遷移前／手動寫入），查詢時視為「不屬於任何一倉」，只在指定 entity 時放行。
    entity: str = ""
    consensus: bool = False
    self_owned: bool = False
    source: str = "seed"
    created_at: float = field(default_factory=time.time)
    # ── 時域（owner 2026-10-23）：雙時域＝「事件時間」與「入庫時間」分開 ──────────
    #: 這條記憶的性質：`preference`（偏好）｜`state`（狀態，會被新值覆蓋）｜
    #: `event`（某時發生的事，過了就過期）｜`rule`（約定／規則）｜空＝未分類
    kind: str = ""
    #: 事件時間軸：從什麼時候開始成立（0＝未指定，用 created_at）
    valid_from: float = 0.0
    #: 到什麼時候為止有效（0＝仍有效）；事件型由提煉給定期限
    valid_to: float = 0.0
    #: 被哪一條取代（狀態更新時填新 id，舊的不刪、可回溯「以前是什麼」）
    superseded_by: str = ""
    #: 它取代了哪幾條（門牌）。舊的那一側的 `superseded_by` 指向新的，這裡是新的指向舊的
    #: ⇒ **兩側各存一個哈希指針**（owner 2026-11-01）：從任何一頭都走得回去，
    #: 也不用改寫舊行來記「我取代了誰」。
    supersedes: tuple[str, ...] = ()
    #: 這條自己的門牌（`fact_key(text)`）。舊資料可能空 ⇒ 讀的時候用 `key_value` 現算。
    key: str = ""
    #: 這條是**聽誰說的**（顯示名，例如「アスター」）：跨角色鏡像進來的那一份就是這樣。
    #: 非空 ⇒ 她提起時要用轉述口吻，不能說成對方親口告訴她的（ADR-0002 那條寫法）。
    hearsay_from: str = ""
    #: 狀態槽位（同一件事只能有一個「現值」；例如「主人正在讀的書」）
    slot: str = ""
    # ── 效用（owner 2026-10-24：對抗「高頻＝有意」的誤判）──────────────────────
    #: 這條被撈出來**而且真的被用進回覆**幾次
    utility: int = 0
    #: 被撈出來但那一輪她沒用／沒回（含旁聽）幾次 —— 高 utility 低 misses 才是「有意」
    misses: int = 0
    #: 最後一次被使用的時間（0＝從沒用過）
    last_used_at: float = 0.0
    #: 寫入期的把握度（提煉模型給的 confidence，0–1）——讀路的重排要靠它代替「線上再問一次模型」
    prior: float = 0.0
    #: 來源軸（`dialogue`／`life`，見 `stream_for_source`）；空＝尚未回填（讀的時候視同 dialogue）
    stream: str = ""
    #: 生活流的主題（preset key 或活動標題）；空＝沒有主題（對話流不填）
    topic: str = ""
    #: **這條是遊戲裡的事嗎**（owner 2026-11-11 的 B3）：`1`＝TRPG 卓上（虛構），`0`＝現實。
    #: 為什麼要有這一維：桌上的一切在資料形狀上就是「主人說／主人做了」（role=user、speaker=owner），
    #: 而 facts 只有 layer（pair/circle/…）沒有場景 ⇒ 現實對話一召回，遊戲裡的事就變成他的真實記憶。
    #: 寫入由蒸餾打標（`distill`），讀取由召回過濾（非 trpg 回合只看 `game=0`）。
    game: int = 0
    #: **這是她寫出來的東西嗎**（owner 2026-11-16：「算她做過的事，但不算她經歷過的事」）。
    #: `1`＝作品／虛構文本（場景、小說、桌上素材）：可以聊「我寫到港口那段」，
    #: 但**不能**當成「我今晚在港口」——不進日記的經歷、不進自傳式記憶、不進主動搭話的素材。
    #: `0`＝她親身經歷的現實（預設）。與 `game` 是兩條軸：桌上玩過（game=1）也可能順手寫了作品（fiction=1）。
    fiction: int = 0

    @property
    def usefulness(self) -> float:
        """效用比（0–1）：被用到／被撈出來的比例。沒有被撈過就回 0。

        「高頻但沒用」＝這個值低；讀路要降權的是它，不是「出現次數」。
        """
        total = int(self.utility) + int(self.misses)
        return (float(self.utility) / total) if total else 0.0

    @property
    def key_value(self) -> str:
        """這條的門牌（存的優先；舊資料沒存過就現算）。"""
        return self.key or fact_key(self.text)

    @property
    def recall_text(self) -> str:
        """她想起這條時看到的那一句：跨角色聽來的加轉述前綴（GLOSSARY.md「轉述」），直接經歷的不動。"""
        if self.hearsay_from:
            return f"（{self.hearsay_from}から聞いた話）{self.text}"
        return self.text

    def with_hearsay(self, name: str) -> "Fact":
        """同一條事實、換成「聽 `name` 說的」那一個視角（不改原本那份）。"""
        return replace(self, hearsay_from=str(name or ""))

    def is_active(self, now: float | None = None) -> bool:
        """這條在「現在」還成立嗎？（valid_to 過了就不成立；被取代也不算）"""
        if self.superseded_by:
            return False
        stamp = float(now if now is not None else time.time())
        return not (self.valid_to and self.valid_to <= stamp)

    def valid_at(self, when: float) -> bool:
        """as-of 查詢：`when` 那一刻這條成立嗎？"""
        start = float(self.valid_from or self.created_at or 0)
        end = float(self.valid_to or 0)
        if start and when < start:
            return False
        return not (end and when >= end)

    def recallable(self) -> bool:
        """**歸屬層**：她能不能想起來。

        她的倉（`blueagent-<entity>` / 本地 SQLite）裡的事都是她經歷過的 ⇒ 她永遠想得起來。
        這一層刻意不做任何過濾：「想不起來」和「不能說」是兩件事，
        以前把它們混在一個 `visible_to` 裡，結果她一個人待著時連自己經歷過的事都撈不到。
        """
        return True

    def disclosable(self, participants: Iterable[str]) -> bool:
        """**披露層**：能不能在「現在這幾個人面前」把這條說出來。

        owner 2026-10-23 拍板：**只看子集**（取消 2026-10-21 的「host 在場／當事人到齊」那道）。

          `在場的人 ⊆ 這條記憶的房間成員`  ⇔  可說

        `participants` 必須**含她自己**（她的回合裡她一定在場）⇒ 她一個人時
        `{她} ⊆ 任何含她的房間` 恆成立 ⇒ 她單獨也能把任何自己的記憶想起來、說給自己聽；
        換成客人單獨在場時 `{她, 客人}` 就對不上「她＋主人」那間房 ⇒ 封口（記得，但不提）。

        `audience=None`＝世界事實，任何人都可聽。
        """
        if self.audience is None:
            return True
        people = {str(person).strip() for person in participants if str(person).strip()}
        return people <= set(self.audience)

    def visible_to(self, participants: Iterable[str], *, host: str = "") -> bool:
        """舊名字：等於 `disclosable`（`host` 參數已不生效，保留給既有呼叫點）。"""
        return self.disclosable(participants)

    def readable(self, participants: Iterable[str]) -> bool:
        """**她這一輪的上下文**收不收這一條。

        owner 2026-11-03「線路 A」：她自己那層（`layer="self"`，日記／活動成果）是**她的域**，
        她永遠想得起來——她就是本體，不需要許可；要不要在這場說出來、對誰說，是**她的決定**
        （靠 prompt 的〔私記〕標記與她的判斷，不再用權限表擋）。

        其餘層照舊走披露閘（`disclosable`：在場 ⊆ 房間）。

        例外（owner 2026-11-16 A.13 降權）：在場有客人（`guest_present`）時，講到主人
        （`OWNER_MENTION`）的私記**不收**。

        ⚠️ 只有**房間裡只有她自己**的那幾條才繞過閘（2026-11-03 review）：self 層不全是她自己的事——
        `reflection.py` 的日記素材是主人在場的對話原文，房間是那幾段的交集（例：`{mira, owner}`）。
        全部放行的話，主人私聊寫成的日記會在客人的聊天裡被召回（實測：Alice 的聊天撈到「オーナーが
        アリスの誕生日サプライズを相談してくれた」）。房間裡有別人的 self 事實照舊走披露閘。
        """
        people = [str(person) for person in participants or ()]
        if str(self.layer or "") == "self" and self.only_hers():
            # A.13（owner 2026-11-16「降權」）：有客人在場時，講到主人的私記不進上下文——
            # 不再把「對客人別說主人的事」只寄託在模型的 prompt 遵循上（誘導話術會套出〔私記〕）。
            # 她自己的、與主人無關的私記照舊想得起來（線路 A 其餘部分不變）。
            if guest_present(people, self.entity) and OWNER_MENTION.search(self.text or ""):
                return False
            return True
        return self.disclosable(people)

    def only_hers(self) -> bool:
        """這一條的房間是不是**只有她自己**（她的活動成果、她自己的筆記）。"""
        if self.audience is None:
            return False                           # 世界可見的照常走披露閘（本來就可說）
        members = {str(who) for who in self.audience}
        me = str(self.entity or "").strip()
        # 沒綁角色的舊資料：房間只有一個人＝就是她自己（分倉已經按角色取）
        return members <= {me} if me else len(members) == 1

    def audience_text(self) -> str:
        return "世界" if self.audience is None else "、".join(sorted(self.audience))

    def to_dict(self, *, participants: Iterable[str] | None = None,
                host: str = "") -> dict:
        visible = True if participants is None else self.disclosable(participants)
        return {"text": self.text, "layer": self.layer,
                "layer_label": LAYER_LABEL.get(self.layer, self.layer),
                "label": self.label, "audience": self.audience_text(),
                "utility": int(self.utility), "misses": int(self.misses),
                "prior": round(float(self.prior or 0.0), 3),
                "usefulness": round(self.usefulness, 3),
                "principal": self.principal, "entity": self.entity,
                "consensus": self.consensus,
                "self_owned": self.self_owned, "visible": visible,
                "recallable": self.recallable(),
                # 取代關係：兩側的哈希指針都露出來（記憶頁要能標「已被取代」，也要能走回舊的）
                "key": self.key_value,
                "supersedes": list(self.supersedes),
                "superseded_by": self.superseded_by,
                "active": self.is_active(),
                "hearsay_from": self.hearsay_from,
                "source": self.source, "created_at": round(self.created_at, 3),
                "stream": self.stream or stream_for_source(self.source),
                "topic": self.topic, "game": int(self.game or 0),
                # 單值槽位（同槽只留一條現值）：提煉要看得到現用的槽位名，記憶頁也能標出來
                "kind": self.kind, "slot": self.slot}

    def to_store_dict(self) -> dict:
        return {"text": self.text, "layer": self.layer,
                "audience": None if self.audience is None else sorted(self.audience),
                "label": self.label, "principal": self.principal,
                "entity": self.entity,
                "consensus": self.consensus, "self_owned": self.self_owned,
                "source": self.source, "created_at": self.created_at,
                "stream": self.stream or stream_for_source(self.source), "topic": self.topic,
                "game": int(self.game or 0),
                # 原文優先：取代關係也要跟著原文走，否則重建索引就丟了指針
                "kind": self.kind, "valid_from": self.valid_from,
                "valid_to": self.valid_to, "superseded_by": self.superseded_by,
                "supersedes": list(self.supersedes), "key": self.key,
                "slot": self.slot, "prior": self.prior}

    @classmethod
    def from_store_dict(cls, raw: dict) -> "Fact":
        audience = raw.get("audience")
        return cls(
            text=str(raw.get("text") or ""),
            layer=str(raw.get("layer") or "private"),
            audience=None if audience is None else frozenset(str(x) for x in audience),
            label=str(raw.get("label") or ""),
            principal=str(raw.get("principal") or ""),
            entity=str(raw.get("entity") or ""),
            consensus=bool(raw.get("consensus")),
            self_owned=bool(raw.get("self_owned")),
            source=str(raw.get("source") or "manual"),
            created_at=float(raw.get("created_at") or time.time()),
            kind=str(raw.get("kind") or ""),
            valid_from=float(raw.get("valid_from") or 0),
            valid_to=float(raw.get("valid_to") or 0),
            superseded_by=str(raw.get("superseded_by") or ""),
            supersedes=parse_keys(raw.get("supersedes")),
            key=str(raw.get("key") or ""),
            slot=str(raw.get("slot") or ""),
            prior=float(raw.get("prior") or 0),
            stream=str(raw.get("stream") or ""),
            topic=str(raw.get("topic") or ""),
            game=int(raw.get("game") or 0),
        )
