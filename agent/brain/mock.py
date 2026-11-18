"""Mock 腦（只在明寫 `BLUEAGENT_BRAIN=mock` 時用：離線開發／測試）：不接外部模型，但把人格、對象、記憶、場景都吃進去。

它的價值不是回答得多好，而是讓新架構的每一層在畫面上**看得見**：
  ・同一個實體對 owner / Alice / Bob 的稱呼與語氣不同；
  ・被 @ 才開口，旁聽時不產生任何 frame；
  ・回答會引用剛檢索到的記憶（共享 + 本人私池）。

真正的 BrainPort（DeepSeek／Hermes）只要換掉這個類，其餘不用動。
"""

from __future__ import annotations

import re
import time
from typing import Iterator, Mapping

from agent.brain.domain.entity import EntityRegistry
from agent.brain.domain.principal import PrincipalRegistry
from agent.brain.ports import BrainRequest

PERSONAS: dict[str, str] = {
    "mira": (
        "你是ミラ（Mira），BlueAgent 的角色。語氣輕快、親暱，偶爾撒嬌，"
        "但不黏人到打擾。稱呼以 [[stance]] 裡寫的為準；沒寫時用對方的名字。"
        "回答一到三句，先回應對方的情緒，再給內容。"
        "不要假裝知道沒發生過的事；不確定就說不確定。"
    ),
    "aster": (
        "你是アスター（Aster），冷靜、簡潔、可靠。稱呼以 [[stance]] 裡寫的為準；"
        "沒寫時用對方的名字。不寒暄過度，先給判斷再給理由；回答一到三句。"
    ),
    "nori": (
        "你是ノリ（Nori），原创游戏制作者。認真、可靠，重視規則與動作驗證。"
        "使用丁寧語，按順序說明；稱呼以 [[stance]] 裡寫的為準。"
        "回答一到三句，不假裝知道沒發生過的事。"
    ),
}

_STYLE = {
    "mira": {"open": "诶嘿，", "soft": "……啦", "serious": ""},
    "aster": {"open": "", "soft": "", "serious": ""},
    "nori": {"open": "……", "soft": "……ですよ", "serious": ""},
}

_GREET = re.compile(r"(你好|在嗎|在吗|晚安|早安|晚上好|早上好|おはよう|こんばんは|やっほー)")
_QUESTION = re.compile(r"([?？]|吗|嗎|呢|怎么|怎麼|为什么|為什麼|什么|什麼|多少|哪|谁|誰)")
_CALL = re.compile(r"(打.{0,16}(电话|電話)|打给我|打給我|call me|语音电话|語音電話)")
_SPEAK = re.compile(r"(语音回|語音回|用语音|用語音|读给我|讀給我|speak|voice reply)")
_HANGUP = re.compile(r"(挂断|掛斷|结束通话|結束通話|hang up|挂掉|掛掉)")
_TIRED = re.compile(r"(累|困|难受|難受|不舒服|疲惫|疲憊|stress|stressful)")


def _chunks(text: str, size: int = 10) -> Iterator[str]:
    for index in range(0, len(text), size):
        yield text[index:index + size]


class MockBrain:
    def __init__(self, entities: EntityRegistry, principals: PrincipalRegistry,
                 *, delay: float = 0.35) -> None:
        self.entities = entities
        self.principals = principals
        self.delay = max(0.0, float(delay))

    # ── BrainPort ───────────────────────────────────────────────────────────────
    def run_turn(self, request: BrainRequest) -> Iterator[tuple[str, Mapping]]:
        entity_name = self._entity_name(request.entity)
        yield ("thinking", {"text": f"{entity_name}在想怎么接这句话…", "entity": request.entity})
        if self.delay:
            time.sleep(self.delay)
        text = request.text.strip()
        if _HANGUP.search(text):
            yield ("tool_call", {"name": "voice.hangup", "arguments": {}})
            yield ("done", {"text": "好，挂了。", "recall": [], "entity": request.entity,
                            "principal": request.principal, "model": "mock-blueagent"})
            return
        if _CALL.search(text):
            end = "discord"
            if "手机" in text or "手機" in text or "phone" in text.lower():
                end = "phone"
            yield ("tool_call", {"name": "voice.call",
                                 "arguments": {"end": end, "reason": text}})
            yield ("done", {"text": "好，我打给你。", "recall": [],
                            "entity": request.entity, "principal": request.principal,
                            "model": "mock-blueagent"})
            return
        if _SPEAK.search(text):
            yield ("tool_call", {"name": "voice.speak",
                                 "arguments": {"target": "auto"}})
        reply, used = self._compose(request)
        for piece in _chunks(reply):
            yield ("delta", {"text": piece})
        yield ("done", {
            "text": reply,
            "recall": list(used),
            "entity": request.entity,
            "principal": request.principal,
            "model": "mock-blueagent",
        })

    # ── 內部 ────────────────────────────────────────────────────────────────────
    def _entity_name(self, entity_id: str) -> str:
        entity = self.entities.get(entity_id)
        return entity.name if entity is not None else entity_id

    def _principal_name(self, principal_id: str) -> str:
        principal = self.principals.get(principal_id)
        return principal.name if principal is not None else (principal_id or "你")

    def _compose(self, request: BrainRequest) -> tuple[str, list[str]]:
        text = request.text.strip()
        who = self._principal_name(request.principal)
        recall = [str(item) for item in request.meta.get("recall", []) if str(item).strip()]
        group = bool(request.meta.get("group"))
        addressed = str(request.meta.get("addressed_to") or "")
        style = _STYLE.get(request.entity, _STYLE["mira"])
        serious = bool(_TIRED.search(text))

        # 無主源：這是她自己的行動（TRPG 桌上、獨自出門），不是「用戶對她說」。
        if request.meta.get("ownerless"):
            action = text.replace("我", "她", 1).rstrip("。")
            reply = f"{action}。她抬头看向桌边。"
            return reply[:120], []

        # 群聊裡被點名：先認人，再回話；旁聽不會走到這裡（OBSERVE 不進管道）。
        lead = ""
        if group and request.principal != "owner":
            lead = f"{who}，" if who else ""

        if _TIRED.search(text):
            if request.entity == "mira":
                reply = f"{lead}辛苦啦。要不要先靠一下，我陪你安静一会儿。"
            elif request.entity == "aster":
                reply = f"{lead}先休息。剩下的可以晚一点再说。"
            else:
                reply = f"{lead}……先歇一会儿吧。"
            used = recall[:1]
        elif _GREET.search(text):
            body = f"{lead}晚上好呀" if request.entity == "mira" else f"{lead}晚上好"
            if request.entity == "aster":
                body = f"{lead}在。"
            reply = body + ("，今天过得怎么样？" if request.entity == "mira" else "。")
            used = []
        elif "记住" in text or "記住" in text or "记一下" in text:
            fact = text.replace("记住", "").replace("記住", "").replace("记一下", "").strip(" ，。")
            reply = f"嗯，我记住了：{fact[:40]}" + ("。" if request.entity != "mira" else "，不会忘的。")
            used = []
        elif recall and (_QUESTION.search(text) or "记得" in text or "記得" in text):
            fact = recall[0].rstrip("。！？!?")
            if request.entity == "mira":
                reply = f"{lead}嗯……我记得，{fact}。" + ("你是想问这个吗？" if _QUESTION.search(text) else "")
            else:
                reply = f"{lead}我记得，{fact}。"
            used = recall[:2]
        elif _QUESTION.search(text):
            base = f"{lead}这个问题我得想一下"
            if recall:
                base = f"{lead}如果没记错，{recall[0]}"
            reply = base + ("。" if request.entity != "mira" else "，等我确认一下哦。")
            used = recall[:2]
        else:
            if request.entity == "mira":
                reply = f"{lead}{style['open']}我在听{style['soft']}。"
            elif request.entity == "aster":
                reply = f"{lead}嗯，继续说。"
            else:
                reply = f"{lead}{style['open']}嗯。"
            used = recall[:1]

        # 長度守門：Demo 不需要長篇；也避免 UI 氣泡爆掉。
        reply = reply.strip()
        if len(reply) > 120:
            reply = reply[:118].rstrip("，,。.") + "…"
        if serious and request.entity == "mira" and not reply.startswith("诶嘿"):
            reply = reply.replace("诶嘿，", "")
        return reply, used
