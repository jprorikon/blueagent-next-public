"""領域標識：**一個角色 ＝ 一個實體（entity），不是一個 session**。

使用者 2026-10-21 拍板的方向：比起「會話 ID」，更應該是「agent 的一個角色持有一個實體 ID」。

所以這一層的第一性對象是 `CharacterEntity`（見 `entity.py`）：

  · **entity**：角色本體。穩定 ID（`char_mira`），跨重啟、跨裝置、跨模型、跨 UI 都不變。
    記憶、關係、情緒、聲音、形象、當前注意力、當前在場，全部掛在它身上。
  · **principal**：這次「是誰在說話」——由源在認證之後解析，不信任 payload 自報。
  · **scope**：這條線的錨。一對一時就是 principal；群場景（一個 Discord 頻道、一張 TRPG 桌）
    是 `group:<scene>`，所有人共用同一條線，每輪另記 speaker。
  · **thread**：實體在「某個場景 × 某個目的」下的一段連續對話，由
    `(scope, entity, scene, purpose)` **確定性推導**。它不是會話：客戶端不送、使用者不開、
    過窗自動換 generation，而記憶不跟著斷。
  · **episode**：thread 內部自動切出來的一段（閒置／跨日／字數／話題邊界），
    是歸檔與壓縮的單位（見 `episode.py`）。

`base` 是穩定線路標識；`with_generation()` 才是具體的一段。這樣「自動切片」不需要任何
使用者動作，而存檔／檢索又能分清先後。
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass

_NON_SLUG = re.compile(r"[^a-z0-9]+")
_MAX_PART = 40


def norm(value: object, *, fallback: str = "x", max_len: int = _MAX_PART) -> str:
    """把任意字串壓成可讀、可比較的 slug（鍵必須穩定且跨端一致）。"""
    text = str(value or "").strip().lower()
    text = _NON_SLUG.sub("-", text).strip("-")
    return (text or fallback)[:max_len]


@dataclass(frozen=True, slots=True)
class ThreadKey:
    """一條「線」的推導鍵。任何一個分量變了，就是另一條線。

    注意 `entity` 在 `scene` 之前：先確定「跟哪個角色」，再確定「在哪個場景」。
    這與舊模型剛好相反（舊模型先有 session，角色只是 session 上的一個欄位）。
    """

    scope: str
    entity: str
    scene: str = "together"
    purpose: str = "chat"

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", norm(self.scope, fallback="owner"))
        object.__setattr__(self, "entity", norm(self.entity, fallback="default"))
        object.__setattr__(self, "scene", norm(self.scene, fallback="together"))
        object.__setattr__(self, "purpose", norm(self.purpose, fallback="chat"))

    @property
    def base(self) -> str:
        payload = "\x1f".join((self.scope, self.entity, self.scene, self.purpose))
        digest = hashlib.blake2s(payload.encode("utf-8"), digest_size=6).hexdigest()
        return f"th_{digest}"

    def with_generation(self, generation: int) -> str:
        return f"{self.base}-{int(generation)}"

    def to_log(self) -> str:
        return f"{self.scope}/{self.entity}/{self.scene}/{self.purpose}"

    def to_dict(self) -> dict:
        return {"scope": self.scope, "entity": self.entity,
                "scene": self.scene, "purpose": self.purpose}

    @classmethod
    def from_dict(cls, raw: dict) -> "ThreadKey":
        return cls(scope=str(raw.get("scope") or "owner"),
                   entity=str(raw.get("entity") or "default"),
                   scene=str(raw.get("scene") or "together"),
                   purpose=str(raw.get("purpose") or "chat"))

    @classmethod
    def from_log(cls, value: str) -> "ThreadKey":
        parts = str(value or "").split("/")
        if len(parts) != 4:
            raise ValueError(f"bad thread key: {value!r}")
        return cls(scope=parts[0], entity=parts[1], scene=parts[2], purpose=parts[3])


def new_turn_id() -> str:
    return f"t_{secrets.token_hex(6)}"


def new_episode_id(entity: str, index: int, thread_id: str = "",
                   started_at: float | None = None) -> str:
    """`ep_<entity>-<index>[-<線>[-<開始時刻短哈希>]]`。

    `index` 是**每一條線自己的**序號（`thread.episode_index`）⇒ 只帶 entity＋index 的話，
    每一條線的第 1 集都叫 `ep_mira-1`（實測 `episodes.jsonl` 同一個 id 出現 63 次）。
    載入時按 id 去重，於是 583 筆只活下來 76 筆（2026-10-27 修）。
    線的後綴讓「同角色、不同線」的集數分得開；不給 thread_id（舊呼叫點／測試）仍回舊格式。

    開始時刻短哈希（2026-10-27 補）：線內序號在封片當下就寫進 `episodes.jsonl`，
    卻要等這一輪結束（`threads.persist()`）才落進 `threads.json` ⇒ 中間崩潰的話，
    重啟後同一條線會再發一次同一個序號（真檔裡有 2 組：`ep_mira-15`、`ep_nori-14`）。
    帶上開始時刻，唯一性就不再靠「序號有沒有存盤」。規則與回填工具共用 `start_hash`。
    """
    head = f"ep_{norm(entity, fallback='default')}-{int(index)}"
    tail = id_fragment(thread_id)
    if not tail:
        return head
    if started_at is None:
        return f"{head}-{tail}"
    return f"{head}-{tail}-{start_hash(started_at)}"


#: 開始時刻短哈希的長度（hex）：只用來分開「同線同序號」的兩集，4 位就夠
START_HASH_LEN = 4


def start_hash(started_at: object) -> str:
    """開始時刻的短哈希。封片與 `tools/backfill_episode_ids.py` 用**同一條**規則。"""
    number = float(str(started_at if started_at is not None else 0) or 0)
    return hashlib.sha1(f"{number:.6f}".encode()).hexdigest()[:START_HASH_LEN]


def id_fragment(value: object, *, keep: int = 28) -> str:
    """把線的 id 壓成 id 可用的片段：非字母數字一律變 `_`，連續的收成一個。

    公開給 `tools/backfill_episode_ids.py` 用：回填要跟新封的片用**同一條**規則，
    兩邊各寫一份遲早會漂。
    """
    out: list[str] = []
    for char in str(value or ""):
        if char.isalnum():
            out.append(char)
        elif out and out[-1] != "_":
            out.append("_")
    return "".join(out).strip("_")[:keep]
