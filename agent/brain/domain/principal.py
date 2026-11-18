"""Principal：**說話的人是誰**。

多 principal 的第一原則：**身分必須由源在認證之後給，不能由 payload 自報。**
否則任何拿到 token 的客戶端都能自稱是另一個人，記憶與關係就全線失守。

這一層只做兩件事：

  · `Principal`：一個可被尋址的人（`owner`、`alice`……）。
  · `PrincipalRegistry`：外部身分（token／Discord user id／App 帳號／TRPG 席位）→ 內部 principal 的映射。
    一個人在不同源上的多個外部 id 可以綁到同一個 principal。**核心帳號在腦端**（owner 2026-11-09）：
    主人是一個 principal，App 帳號、Discord 帳號都是綁在它身上的外部身分（綁定見 `brain/app/identity.py`）。

群場景（一個 Discord 頻道、一張 TRPG 桌）仍然是一條線，但每個事件帶自己的 principal；
線的歸屬用 `scope = group:<scene>`，人員記在 `Thread.participants`。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from agent.brain.domain.ids import norm


@dataclass(frozen=True, slots=True)
class Principal:
    id: str
    name: str = ""
    owner: bool = False
    #: owner / guest（從屬客戶端）
    role: str = "owner"
    discord_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", norm(self.id, fallback="owner"))
        object.__setattr__(self, "name", str(self.name or self.id).strip())
        object.__setattr__(self, "role", str(self.role or "owner").strip().lower())
        object.__setattr__(self, "discord_id", str(self.discord_id or "").strip())

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "owner": self.owner,
                "role": self.role, "discord_id": self.discord_id}


class PrincipalRegistry:
    def __init__(self, *, default: str = "owner") -> None:
        self._default = norm(default, fallback="owner")
        self._items: dict[str, Principal] = {self._default: Principal(self._default, owner=True)}
        self._by_external: dict[tuple[str, str], str] = {}
        self._by_token: dict[str, str] = {}

    def register(self, principal: Principal, *, replace: bool = False) -> Principal:
        if principal.id in self._items and not replace:
            raise ValueError(f"principal already registered: {principal.id}")
        self._items[principal.id] = principal
        return principal

    def get(self, principal_id: str) -> Principal | None:
        return self._items.get(norm(principal_id, fallback=""))

    def require(self, principal_id: str) -> Principal:
        principal = self.get(principal_id)
        if principal is None:
            raise KeyError(f"unknown principal: {principal_id}")
        return principal

    def default(self) -> str:
        return self._default

    def all(self) -> tuple[Principal, ...]:
        return tuple(self._items.values())

    def names(self) -> "PrincipalNames":
        """principal id → 顯示名的**活視圖**：之後才註冊的人（核可的客人）也查得到。"""
        return PrincipalNames(self)

    # ── 外部身分綁定：源在認證後呼叫 ─────────────────────────────────────────────
    def bind(self, source: str, external_id: str, principal_id: str) -> None:
        """把某個源上的外部 id（Discord user id／TRPG 席位／裝置）綁到 principal。"""
        src = norm(source, fallback="")
        ext = str(external_id or "").strip()
        who = norm(principal_id, fallback="")
        if not (src and ext and who):
            raise ValueError("bind() 需要 source / external_id / principal_id")
        if who not in self._items:
            raise KeyError(f"unknown principal: {who}")
        self._by_external[(src, ext)] = who

    def bind_token(self, token: str, principal_id: str) -> None:
        """Bearer token → principal。只在認證通過之後呼叫。"""
        tok = str(token or "").strip()
        who = norm(principal_id, fallback="")
        if not (tok and who):
            raise ValueError("bind_token() 需要 token / principal_id")
        if who not in self._items:
            raise KeyError(f"unknown principal: {who}")
        self._by_token[tok] = who

    def resolve(self, *, source: str, external_id: str = "", token: str = "",
                fallback: str = "") -> str:
        tok = str(token or "").strip()
        if tok and tok in self._by_token:
            return self._by_token[tok]
        ext = str(external_id or "").strip()
        if ext:
            hit = self._by_external.get((norm(source, fallback=""), ext))
            if hit:
                return hit
        who = norm(fallback, fallback="") if fallback else ""
        return who or self._default

    def unbind(self, source: str, external_id: str) -> bool:
        """拿掉一條外部身分綁定（管理頁解除綁定用）。回有沒有拿掉。"""
        return self._by_external.pop((norm(source, fallback=""), str(external_id or "").strip()),
                                     None) is not None

    def lookup(self, source: str, external_id: str) -> str:
        """某個源上的外部 id 綁到誰；**沒綁就回空字串**（不像 `resolve` 會退回預設的主人）。"""
        ext = str(external_id or "").strip()
        if not ext:
            return ""
        return self._by_external.get((norm(source, fallback=""), ext), "")

    def externals(self, principal_id: str, source: str) -> list[str]:
        """這個 principal 在某個源上綁了哪些外部 id（出站用：她要找主人的 App 帳號／Discord id）。"""
        who = norm(principal_id, fallback="")
        src = norm(source, fallback="")
        return [ext for (bound_src, ext), bound in self._by_external.items()
                if bound_src == src and bound == who]

    def bindings(self) -> list[dict]:
        return [{"source": src, "external_id": ext, "principal": who}
                for (src, ext), who in sorted(self._by_external.items())]

    def snapshot(self) -> list[dict]:
        return [principal.to_dict() for principal in self._items.values()]


class PrincipalNames(Mapping):
    """`PrincipalRegistry` 的唯讀名字表（`names.get("owner") == "主人"`）。

    以前各處寫死 `{"owner": "主人", "alice": "Alice", "bob": "Bob"}`——demo 的人名進了
    生產，真的客人反而沒有名字。這裡永遠跟著註冊表走。
    """

    def __init__(self, registry: PrincipalRegistry) -> None:
        self._registry = registry

    def __getitem__(self, principal_id: str) -> str:
        principal = self._registry.get(principal_id)
        if principal is None:
            raise KeyError(principal_id)
        return principal.name

    def __iter__(self):
        return iter([principal.id for principal in self._registry.all()])

    def __len__(self) -> int:
        return len(self._registry.all())
