"""關係認知：人與人之間的邊、圈子，以及記憶的可見域。

這不是「社交圖譜 API」，而是**記憶隔離的依據**。三個概念：

    Relation  A–B 的一條邊：家人／同事／朋友……＋稱呼、親密度、信任。
    Circle    一群人的集合（家人圈、同事圈）→ 可以直接當一條記憶的 audience。
    Audience  一條記憶「允許哪些人知道」。`None`＝世界（所有人）。

檢索的硬規則（見 `memory/in_memory.py`）：

    對在場參與者 P 與記憶可見域 A：
        P ⊆ A  ⇔ 可讀
    任何一個在場的人不在 A 裡，這條記憶就不進 prompt、也不該被說出來。

例：A=主人、B=Alice（家人）、C=Bob（同事）
    家人記憶 A={主人,Alice}：主人在場可讀、Alice 在場可讀、Bob 在場不可讀。
    同事記憶 A={主人,Bob}：主人在場可讀、Bob 在場可讀、Alice 在場不可讀。
    共識記憶 A={主人,Alice,Bob}：三個人在場可讀；只有兩人時也讀得到（兩人都知道），
    但不會因此讓不在場的人知道。
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.brain.domain.ids import norm


@dataclass(frozen=True, slots=True)
class Relation:
    """一條無向邊。`label` 是給人看的稱呼（妹妹／同事），`kind` 給規則用。"""

    a: str
    b: str
    kind: str
    label: str = ""
    strength: float = 0.5
    trust: float = 0.5

    def __post_init__(self) -> None:
        object.__setattr__(self, "a", norm(self.a, fallback=""))
        object.__setattr__(self, "b", norm(self.b, fallback=""))
        object.__setattr__(self, "kind", norm(self.kind, fallback="acquaintance"))
        object.__setattr__(self, "label", str(self.label or self.kind).strip())

    def involves(self, who: str) -> bool:
        return norm(who, fallback="") in (self.a, self.b)

    def other(self, who: str) -> str:
        token = norm(who, fallback="")
        return self.b if token == self.a else self.a

    def to_dict(self) -> dict:
        return {"a": self.a, "b": self.b, "kind": self.kind, "label": self.label,
                "strength": round(self.strength, 2), "trust": round(self.trust, 2)}


@dataclass(frozen=True, slots=True)
class Circle:
    """一個可見域的共同體。`members` 就是記憶的 audience。"""

    id: str
    name: str
    members: frozenset[str]
    kind: str = "circle"

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", norm(self.id, fallback="circle"))
        object.__setattr__(self, "name", str(self.name or self.id).strip())
        object.__setattr__(self, "members", frozenset(norm(m, fallback="") for m in self.members))

    def includes(self, participants: set[str]) -> bool:
        return set(participants) <= set(self.members)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind,
                "members": sorted(self.members)}


class SocialGraph:
    def __init__(self) -> None:
        self._relations: list[Relation] = []
        self._circles: dict[str, Circle] = {}

    # ── 建圖 ────────────────────────────────────────────────────────────────────
    def add_relation(self, a: str, b: str, kind: str, *, label: str = "",
                     strength: float = 0.5, trust: float = 0.5) -> Relation:
        relation = Relation(a, b, kind, label=label, strength=strength, trust=trust)
        if relation.a == relation.b:
            raise ValueError("relation 需要兩個不同的人")
        if self.relation(relation.a, relation.b) is None:
            self._relations.append(relation)
        return relation

    def add_circle(self, circle_id: str, name: str, members, *, kind: str = "circle") -> Circle:
        circle = Circle(id=circle_id, name=name, members=frozenset(members), kind=kind)
        self._circles[circle.id] = circle
        return circle

    # ── 查詢 ────────────────────────────────────────────────────────────────────
    def relation(self, a: str, b: str) -> Relation | None:
        left, right = norm(a, fallback=""), norm(b, fallback="")
        for relation in self._relations:
            if {relation.a, relation.b} == {left, right}:
                return relation
        return None

    def relations_of(self, who: str) -> tuple[Relation, ...]:
        token = norm(who, fallback="")
        return tuple(relation for relation in self._relations if relation.involves(token))

    def circles_of(self, who: str) -> tuple[Circle, ...]:
        token = norm(who, fallback="")
        return tuple(circle for circle in self._circles.values() if token in circle.members)

    def circle(self, circle_id: str) -> Circle | None:
        return self._circles.get(norm(circle_id, fallback=""))

    def members(self, circle_id: str) -> frozenset[str]:
        circle = self.circle(circle_id)
        return circle.members if circle else frozenset()

    def describe(self, who: str, names: dict[str, str] | None = None) -> dict:
        token = norm(who, fallback="")
        names = names or {}
        relations = []
        for relation in self.relations_of(token):
            other = relation.other(token)
            relations.append({"who": other, "name": names.get(other, other),
                              "kind": relation.kind, "label": relation.label,
                              "strength": relation.strength, "trust": relation.trust})
        circles = [{"id": circle.id, "name": circle.name, "kind": circle.kind,
                    "members": sorted(circle.members)} for circle in self.circles_of(token)]
        return {"who": token, "relations": relations, "circles": circles}

    def relation_line(self, a: str, b: str, names: dict[str, str] | None = None) -> str:
        relation = self.relation(a, b)
        if relation is None:
            return ""
        names = names or {}
        return f"{names.get(norm(a), a)} 与 {names.get(norm(b), b)}：{relation.label}"

    def snapshot(self) -> dict:
        return {
            "relations": [relation.to_dict() for relation in self._relations],
            "circles": [circle.to_dict() for circle in self._circles.values()],
        }
