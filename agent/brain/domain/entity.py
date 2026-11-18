"""角色實體：整座橋的**第一性對象**。

一個角色＝一個 `CharacterEntity`，持有一個穩定 ID。它不隨連線、裝置、模型、UI 改變。
所有「她是誰」的東西都掛在這裡：

    id        穩定實體 ID（`char_mira`）——記憶、存檔、關係、分析的 join key
    soul      人格檔（soul/<id>.md）
    voice     TTS 音色／口型
    model     可選的專屬後端

比實體更高一層的只有 `principal`（對方是誰）。比實體更低一層的是 `thread`（場景視圖）
與 `episode`（自動切片）。**沒有 session 這一層。**

為什麼要實體而不是會話：
  · 同一個角色可以同時在手機、Discord、房間、TRPG 桌上「在場」——那是同一個存在，
    不是四個會話；注意力只有一份（見 `runtime/entity_store.py`），記憶只有一份。
  · 換裝置／重連／App 重裝不應該產生新身分；實體 ID 讓「她還是她」變成預設。
  · 換模型、換聲音、換人格版本，實體不變，因此記憶與關係可以延續。
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.brain.domain.ids import ThreadKey, norm


@dataclass(frozen=True, slots=True)
class CharacterEntity:
    """一個可被尋址的角色。`id` 是產品級主鍵，**不要**用顯示名當主鍵。"""

    id: str
    name: str = ""
    soul: str = ""
    voice: str = ""
    model: str = ""
    default: bool = False
    #: 她自己的叫名（群聊「有沒有在叫她」就靠這個）。**別的角色名不該在這裡**——
    #: 否則喊「ノリ」會叫醒 mira（2026-10-22 的 bug）。
    nicknames: tuple = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", norm(self.id, fallback="default"))
        object.__setattr__(self, "name", str(self.name or self.id).strip())
        object.__setattr__(self, "soul", str(self.soul or "").strip())
        object.__setattr__(self, "voice", str(self.voice or "").strip())
        object.__setattr__(self, "nicknames",
                           tuple(str(item).strip() for item in (self.nicknames or ())
                                 if str(item).strip()))
        object.__setattr__(self, "model", str(self.model or "").strip())

    def thread_key(self, principal: str, scene: str = "together", purpose: str = "chat") -> ThreadKey:
        """一對一線的鍵。群場景請由源給 `scope="group:<scene>"` 再建 `ThreadKey`。"""
        return ThreadKey(scope=principal, entity=self.id, scene=scene, purpose=purpose)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "soul": self.soul,
                "voice": self.voice, "model": self.model, "default": self.default}


class EntityRegistry:
    """角色實體註冊表。組合根在啟動時載入 `soul/`，未知實體一律落到 default。

    這張表是「哪個角色存在」的唯一真相；舊模型裡散在 `SOUL_DIR` 掃描、`EAR_PERSONA['soul']`、
    `VOICE_CHARACTER` 環境變數裡的三份，都應該收斂到這裡。
    """

    def __init__(self) -> None:
        self._items: dict[str, CharacterEntity] = {}
        self._default: str = ""

    def register(self, entity: CharacterEntity, *, replace: bool = False) -> CharacterEntity:
        if entity.id in self._items and not replace:
            raise ValueError(f"entity already registered: {entity.id}")
        self._items[entity.id] = entity
        if entity.default or not self._default:
            self._default = entity.id
        return entity

    def get(self, entity_id: str) -> CharacterEntity | None:
        return self._items.get(norm(entity_id, fallback=""))

    def require(self, entity_id: str) -> CharacterEntity:
        entity = self.get(entity_id)
        if entity is None:
            raise KeyError(f"unknown entity: {entity_id}")
        return entity

    def default(self) -> CharacterEntity | None:
        return self._items.get(self._default) if self._default else None

    def all(self) -> tuple[CharacterEntity, ...]:
        return tuple(self._items.values())

    def ids(self) -> frozenset[str]:
        return frozenset(self._items)

    def snapshot(self) -> list[dict]:
        return [entity.to_dict() for entity in self.all()]
