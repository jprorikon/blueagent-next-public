"""實體、人、關係圖的**生產初值**：只建資料，不接線。

只有三個角色與主人。客人由 GuestStore 核可時註冊（人、關係邊、立場都在那裡補）。
2026-10-25 前這裡還註冊了 demo 的 alice／bob 與他們的家人／同事圈——每次開機都進生產；
那些假資料現在只在 `tests/demo_people.py`。
"""

from __future__ import annotations

from agent.brain.domain.entity import CharacterEntity, EntityRegistry
from agent.brain.domain.principal import Principal, PrincipalRegistry
from agent.brain.domain.social import SocialGraph


def build_entities() -> EntityRegistry:
    entities = EntityRegistry()
    entities.register(CharacterEntity("mira", "ミラ", default=True,
                                      soul="mira.md", voice="mira",
                                      nicknames=("ミラ", "ミラ", "mira")))
    entities.register(CharacterEntity("aster", "アスター", soul="aster.md", voice="aster",
                                      nicknames=("アスター", "アスター", "アスター", "aster")))
    entities.register(CharacterEntity("nori", "ノリ", soul="nori.md", voice="nori",
                                      nicknames=("ノリ", "ノリ", "nori", "ノリ")))
    return entities


def build_principals() -> PrincipalRegistry:
    principals = PrincipalRegistry(default="owner")
    principals.register(Principal("owner", "主人", owner=True), replace=True)
    return principals


def build_social() -> SocialGraph:
    """空的關係圖：人↔人的邊只來自客人核可（`GuestStore`），不預設任何人。"""
    return SocialGraph()
