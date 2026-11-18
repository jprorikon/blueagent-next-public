"""測試用的假人物與種子記憶（2026-10-25 從生產搬出來）。

生產只有三個角色與主人；客人由 GuestStore 核可時才出現。可是大量測試需要一個固定的
小世界來驗證披露規則：主人的妹妹 Alice、同事 Bob、家人圈／同事圈、幾條各層的事實。
這些以前寫在 `brain/demo/{entities,stance_seed,seed}.py`，**每次開機都進生產**；
現在只在這裡，由 `demo_common.build()` 裝進測試用的 Demo。
"""

from __future__ import annotations

from agent.brain.domain.principal import Principal

PEOPLE = (("alice", "Alice"), ("bob", "Bob"))


def install_people(principals, social, stances=None) -> None:
    """Alice／Bob、他們跟主人的關係圈、三個角色對他們的立場。"""
    for ident, name in PEOPLE:
        if principals.get(ident) is None:
            principals.register(Principal(ident, name))
    social.add_relation("owner", "alice", "family", label="家人（妹妹）",
                        strength=0.9, trust=0.95)
    social.add_relation("owner", "bob", "colleague", label="同事",
                        strength=0.55, trust=0.6)
    social.add_relation("alice", "bob", "acquaintance", label="通过主人认识",
                        strength=0.35, trust=0.45)
    social.add_circle("family", "家人", {"owner", "alice"})
    social.add_circle("work", "同事", {"owner", "bob"})
    social.add_circle("room", "常在场的三个人", {"owner", "alice", "bob"},
                      kind="scene")
    if stances is None:
        return
    stances.seed("mira", "alice", weights={"friend": 45},
                 notes={"friend": "主人的妹妹；亲切，但不越界。"}, trust=0.75)
    stances.seed("mira", "bob", weights={"colleague": 30},
                 notes={"colleague": "主人的同事；礼貌，保持距离。"}, trust=0.55)
    stances.seed("aster", "alice", weights={"friend": 25}, trust=0.6)
    stances.seed("aster", "bob", weights={"colleague": 25}, trust=0.55)
    stances.seed("nori", "alice", weights={"friend": 30}, trust=0.6)
    stances.seed("nori", "bob", weights={"colleague": 20}, trust=0.5)


def seed_facts(memory, entity: str = "mira") -> None:
    """種子記憶：世界／私密／家人圈／同事圈／她自己／共識各幾條（audience 含她自己）。"""
    me = str(entity or "mira")

    def fact(text: str, layer: str, audience, label: str,
             principal: str = "", consensus: bool = False,
             self_owned: bool = False) -> None:
        memory.add(text, layer=layer, audience=audience, label=label,
                   principal=principal, consensus=consensus,
                   self_owned=self_owned, source="seed", entity=me)

    fact("这间房子是蓝灰色调的，窗边有一盆薄荷。", "world", None, "世界")
    fact("主人怕冷，空调一般开在 26 度。", "world", None, "世界")

    fact("主人准备给 Alice 一个生日惊喜，先别让她知道。", "private",
         {me, "owner"}, "私密", principal="owner")
    fact("主人最近睡得很晚。", "private", {me, "owner"}, "私密", principal="owner")

    fact("他们周末要一起回老家。", "circle", {me, "owner", "alice"}, "家人")
    fact("Alice 是主人的妹妹。", "circle", {me, "owner", "alice"}, "家人")
    fact("Alice 喜欢猫，但猫毛过敏。", "circle", {me, "owner", "alice"}, "家人")

    fact("BlueAgent 下个月要上线。", "circle", {me, "owner", "bob"}, "同事")
    fact("Bob 说话很直接，喜欢开玩笑。", "circle", {me, "owner", "bob"}, "同事")
    fact("Bob 负责后端，最近在赶进度。", "circle", {me, "owner", "bob"}, "同事")

    fact("她自己在 TRPG 桌上认识了一个叫「灰鸦」的冒险者。", "self",
         {me, "owner", "alice", "bob"}, "她的经历", self_owned=True)
    fact("她最近在准备自己的 TRPG 角色卡，还没决定职业。", "self",
         {me, "owner", "alice", "bob"}, "她的经历", self_owned=True)

    fact("上周三个人一起吃了饭，说好下次再聚。", "consensus",
         {me, "owner", "alice", "bob"}, "共识", consensus=True)
    fact("大家都同意不在工作时间里聊太久。", "consensus",
         {me, "owner", "alice", "bob"}, "共识", consensus=True)


def install(demo, *, seed: bool = True) -> None:
    """把測試小世界裝進一個剛 `build()` 好的 Demo（人物＋關係＋立場＋空庫時的種子）。"""
    install_people(demo.principals, demo.social, getattr(demo, "stances", None))
    if not seed:
        return
    memory = demo.memory
    target = getattr(memory, "local", memory)            # HindsightMemory 的原文在 .local
    snapshot = target.snapshot() if hasattr(target, "snapshot") else {}
    if not any(row.get("source") == "seed" for row in (snapshot or {}).get("facts", [])):
        seed_facts(memory)


__all__ = ["PEOPLE", "install", "install_people", "seed_facts"]
