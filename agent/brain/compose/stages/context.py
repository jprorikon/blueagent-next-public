"""階段之間的資料契約（review.md 2026-10-25 P2：`env.meta` 是隱式字串契約）。

以前 12 個階段靠 `env.meta["某個字串"]` 互相傳東西：鍵名、預設值、誰寫誰讀都只存在於各處
程式碼裡，漏寫或改名要到執行期才發現。這裡把**每一個鍵**宣告一次：

    屬於哪一段（誰的產物）、預設值、准許哪幾個階段寫

用法（新程式碼一律這樣寫）：

    turn = TurnContext.of(env)
    system = turn.persona.persona          # 讀：沒寫過就拿宣告的預設值
    turn.output.reply_text = reply         # 寫：落在同一個 env.meta 字典裡
    turn.output.add_tool_result(result)    # 清單型的用專用方法（setdefault + append）

**相容**：這是同一個 `env.meta` 字典上的有型別視圖，不是另一份資料——還沒遷移的階段照樣
用 `env.meta[...]`，兩邊看到的是同一個值。`tests/test_stage_contract.py` 會掃所有階段：
用到沒宣告的鍵、或不在 writers 裡的階段去寫某個鍵，就紅。
"""

from __future__ import annotations

from typing import Any, Callable

_MISSING = object()


class Field:
    """一個鍵：預設值（或工廠）＋准許寫的階段模組（`app/stages/<模組>.py`）。"""

    def __init__(self, default: Any = None, *, factory: Callable[[], Any] | None = None,
                 writers: tuple[str, ...] = (), doc: str = "") -> None:
        self.default = default
        self.factory = factory
        self.writers = tuple(writers)
        self.doc = doc
        self.key = ""

    def __set_name__(self, owner, name: str) -> None:
        self.key = name

    def __get__(self, section, objtype=None):
        if section is None:
            return self
        value = section._meta.get(self.key, _MISSING)
        if value is not _MISSING:
            return value
        return self.factory() if self.factory is not None else self.default

    def __set__(self, section, value) -> None:
        section._meta[self.key] = value


class Section:
    """一段產物；欄位都是 `Field`，讀寫落在同一個 `env.meta`。"""

    def __init__(self, meta: dict) -> None:
        self._meta = meta

    def has(self, key: str) -> bool:
        """這個鍵有沒有被寫過（區分「沒寫」與「寫了空值」）。"""
        return key in self._meta

    @classmethod
    def fields(cls) -> dict[str, Field]:
        out: dict[str, Field] = {}
        for klass in reversed(cls.__mro__):
            out.update({name: value for name, value in vars(klass).items()
                        if isinstance(value, Field)})
        return out


class IntakeOut(Section):
    """進線（`intake`）：原文與看圖結果。"""

    original = Field("", writers=("intake",), doc="正規化之前的原文")
    image_note = Field("", writers=("intake",), doc="看圖：圖讀成的文字")
    image_path = Field("", writers=("intake",), doc="看圖：圖檔位置")


class RecallOut(Section):
    """回想（`recall`）：被動召回（已過披露規則）與子代理筆記。"""

    recall = Field(factory=list, writers=("recall",), doc="召回的事實文字（進 prompt）")
    recall_detail = Field(factory=list, writers=("recall",), doc="召回明細（層、房間）")
    recall_shared = Field(factory=list, writers=("recall",),
                          doc="與 recall 一一對應：這一場的人是否也知道（common-ground 第 2 步）")
    banks = Field(factory=list, writers=("recall",), doc="這一輪查了哪些記憶倉")
    participants = Field(factory=list, writers=("recall", "persona"),
                         doc="在場名冊（recall 先寫、persona 依名冊覆寫）")
    subagent_notes = Field(factory=list, writers=("recall",), doc="子代理／提前想的筆記")
    prethink = Field("", writers=("recall",), doc="這一輪用掉的提前想（原文）")


class PersonaOut(Section):
    """人格段（`persona`）：人格／身分／立場／user.md／模式與權限。之後的階段只讀。"""

    persona = Field("", writers=("persona",), doc="system 提示詞（人格＋身分＋群聊規則）")
    soul_block = Field("", writers=("persona",))
    core_block = Field("", writers=("persona",))
    user_block = Field("", writers=("persona",), doc="這個人的那一頁 user.md（每輪都進）")
    self_log_block = Field("", writers=("persona",))
    mood_block = Field("", writers=("persona",))
    #: in-time 現況（owner 2026-11-03）：她**此刻**在做什麼（活動／進度／剛完成）。
    #: 與「歷史」是不同的東西：歷史是跟誰說過什麼，這是同一條時間軸上她自己的事。
    presence_block = Field("", writers=("persona",), doc="現況段（她此刻在做什麼）")
    #: 同一條線裡**別人**說的話（群聊背景）。跟「當前對象的歷史」分開，避免串台。
    ambient = Field(factory=list, writers=("persona",), doc="其他人在同一條線的發言（背景）")
    #: 時間感（`runtime/inner/timeframe.py`）：現在幾點、這條線第幾輪、心跳跳了幾次。
    clock_block = Field("", writers=("persona",), doc="時間感段（現在時刻／輪次／跳數）")
    skills_block = Field("", writers=("persona",))
    stance = Field("", writers=("persona",))
    speak_lang = Field("", writers=("persona",))
    mode = Field(None, writers=("persona",), doc="chat／work（由發起角色固定）")
    mode_note = Field("", writers=("persona",))
    room_note = Field("", writers=("persona",), doc="App 房裡她上次看過之後的發言（adapter 從 App 取）")
    group = Field(False, writers=("persona",))
    ownerless = Field(False, writers=("persona",))
    is_owner = Field(True, writers=("persona",), doc="token 那一側的身分（不是模型自述）")
    capabilities = Field(None, writers=("persona",), doc="這一輪放行的能力（例如 delegate）")
    entity_name = Field(None, writers=("persona",), doc="沒寫過＝用決策裡的 entity id")
    principal_name = Field(None, writers=("persona",), doc="沒寫過＝用決策裡的 principal id")
    names = Field(factory=dict, writers=("persona",), doc="principal id → 顯示名")
    transcript = Field(factory=list, writers=("persona",), doc="滾動窗（給 brain/messages）")
    carry_summary = Field("", writers=("persona",), doc="上一段封片的承接摘要")
    elsewhere = Field(factory=list, writers=("persona",), doc="跨線餘溫：她別條活線剛剛的幾輪（帶渠道來源）")
    #: 未渡しの依頼（`runtime/inner/pending_delivery.py`）：主人要她交付、她還沒交的東西。
    #: 放在本輪段——它每輪都可能變（交了／被追問），塞固定段會打掉前綴快取。
    delivery_block = Field("", writers=("persona",), doc="未交的交付（[[todo]]）")


class StateOut(Section):
    """狀態引出（`state_elicit`）：這一輪的心情／狀態。"""

    state = Field(None, writers=("state_elicit",))
    state_block = Field("", writers=("state_elicit",))
    social_condition = Field("", writers=("state_elicit",))


class Overrides(Section):
    """呼叫端／測試可預先放進來的覆寫（管線裡沒有階段會寫）。"""

    game_block = Field("", doc="遊戲段；沒有就用 runtime.game_context 現在的局")
    tts_stream = Field(None, doc="這一輪要不要邊生成邊合成；None＝看 BLUEAGENT_TTS_STREAM")


class TurnOutput(Section):
    """大腦以後的產物：回覆、語音狀態、工具結果（`brain` 寫；`memory_write`／`egress` 讀）。"""

    reply_text = Field("", writers=("brain",))
    spoken_text = Field("", writers=("brain",), doc="已經串流唸出去的部分（語音出口不重唸）")
    force_voice = Field(False, writers=("brain", "persona"),
                        doc="這一輪一定出聲（payload want_voice 或 voice.speak 工具）")
    voice_target = Field("", writers=("brain",))
    voice_interrupted = Field(False, writers=("brain",), doc="被插話（barge-in）打斷")
    tool_results = Field(factory=list, writers=("brain",))
    tool_error = Field("", writers=("brain",))
    memory_hits = Field(factory=list, writers=("brain",), doc="工具撈出的事實（效用記帳用）")
    stickers = Field(factory=list, writers=("brain",), doc="她這一輪送出的表情（意思；寫進對話紀錄用）")
    sticker_ids = Field(factory=list, writers=("brain",),
                        doc="她這一輪送出的表情 id（英文標記 `[[sticker:<id>]]` 用）")
    motion_sent = Field(False, writers=("brain",), doc="這一輪的動作已經處理過（發了，或確定不發）⇒ 語音出口不再補發")

    def add_tool_result(self, result: dict) -> None:
        self._meta.setdefault("tool_results", []).append(result)

    def add_sticker(self, meaning: str, sticker_id: str = "") -> None:
        self._meta.setdefault("stickers", []).append(meaning)
        if sticker_id:
            self._meta.setdefault("sticker_ids", []).append(sticker_id)

    def add_memory_hits(self, texts: list[str]) -> None:
        self._meta.setdefault("memory_hits", []).extend(texts)


SECTIONS: tuple[type[Section], ...] = (IntakeOut, RecallOut, PersonaOut, StateOut,
                                       Overrides, TurnOutput)


def declared_fields() -> dict[str, Field]:
    """所有宣告過的鍵（鍵名不准在兩段重複）。"""
    out: dict[str, Field] = {}
    for section in SECTIONS:
        for name, field in section.fields().items():
            if name in out:
                raise RuntimeError(f"env.meta 鍵 {name!r} 在兩段重複宣告")
            out[name] = field
    return out


class TurnContext:
    """一輪在管線裡的有型別視圖（底下就是 `env.meta`）。"""

    def __init__(self, meta: dict) -> None:
        self.meta = meta
        self.intake = IntakeOut(meta)
        self.recall = RecallOut(meta)
        self.persona = PersonaOut(meta)
        self.state = StateOut(meta)
        self.overrides = Overrides(meta)
        self.output = TurnOutput(meta)

    @classmethod
    def of(cls, env) -> "TurnContext":
        return cls(env.meta)


__all__ = ["Field", "IntakeOut", "Overrides", "PersonaOut", "RecallOut", "SECTIONS", "Section",
           "StateOut", "TurnContext", "TurnOutput", "declared_fields"]
