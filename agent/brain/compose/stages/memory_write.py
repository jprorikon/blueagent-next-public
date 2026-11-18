"""記憶寫入與效用記帳：這一輪留下什麼、撈出來的有沒有真的用到。"""

from __future__ import annotations

from agent.brain.compose.stages.context import TurnContext
from agent.brain.memory.facts import grams
from agent.brain.domain.thread import TurnRecord


class MemoryUsageStage:
    """記憶效用記帳（owner 2026-10-24）。

    為什麼要有：`memory.search` 撈出來的事實如果這一輪**根本沒被用進回覆**，
    那就是噪音；下次還因為「出現在結果裡」被撈出來，就會變成自我強化的迴圈
    （「角色卡」那件事就是這樣滾起來的）。這裡只記帳，不改分數——先有資料再談門檻。

    判據：回覆裡出現這條事實**三分之一以上的二字詞**就算「用到」。
    """

    name = "memory.usage"

    def __init__(self, usage=None, *, overlap: float = 0.34) -> None:
        self.usage = usage
        self.overlap = max(0.0, min(1.0, float(overlap)))

    def run(self, env, ctx):
        if self.usage is None:
            return None
        output = TurnContext.of(env).output
        hits = [str(text) for text in (output.memory_hits or []) if str(text).strip()]
        if not hits:
            return None
        reply = str(output.reply_text or "")
        used: list[str] = []
        missed: list[str] = []
        for text in dict.fromkeys(hits):                    # 去重、保序
            (used if _overlaps(text, reply, self.overlap) else missed).append(text)
        try:
            self.usage(used=used, missed=missed)
        except Exception as exc:                            # noqa: BLE001 - 記帳不能擋回話
            print(f"[usage] 記帳失敗（{type(exc).__name__}: {exc}）", flush=True)
        return None


def _overlaps(hit: str, reply: str, threshold: float) -> bool:
    """回覆裡出現這條事實多少比例的二詞（0–1）。太短的事實要求整串出現。"""
    body = str(reply or "").strip()
    if not body:
        return False
    hit_grams = {gram for gram in grams(str(hit or "")) if len(gram) >= 2}
    if not hit_grams:
        return False
    if len(hit_grams) <= 2:                                 # 短事實：要整串在裡面
        return str(hit).strip() in body
    return len(hit_grams & grams(body)) / len(hit_grams) >= threshold


class MemoryWriteStage:
    name = "memory.write"

    def __init__(self, userfile=None) -> None:
        #: 每個 user 一頁（檔案層）。有它才做自動累積。
        self.userfile = userfile

    def run(self, env, ctx):
        turn = TurnRecord(
            turn_id=env.turn_id, role="user", text=env.text, source=env.event.source,
            at=env.event.at, speaker=env.decision.principal,
            addressed_to=env.event.addressed_to,
        )
        people = tuple(env.event.participants or (env.decision.principal,))
        ctx.ports.record(env.decision.scope, turn, participants=people)
        if self.userfile is not None:
            self._remember_in_userfile(env, turn, people)
        return None

    def _remember_in_userfile(self, env, turn, people) -> None:
        """把「他自己說的、關於他自己的」**穩定事實**寫進他的頁；別人的事不寫。

        owner 2026-10-22 第 ④ 步：`recent` 不再重貼對話。三條規矩——
          ① 只收**穩定事實**（`rule`：喜歡／討厭／住哪／叫我X；`consensus`：說好／大家都），
             「記住這句」那種整段引用（`explicit`）只進記憶層，不進 recent；
          ② 這句還在滾動窗裡（`transcript`）時**先不寫**：模型這一輪已經看得到原文，
             寫進來等於把同一句話貼兩次；
          ③ 一輪最多寫 2 條（避免一次吐一串把 recent 塞滿）。
        """
        from agent.brain.memory.fact_rules import facts_for_turn
        facts = facts_for_turn(env.decision.scope, turn, participants=people)
        principal = env.decision.principal
        written = 0
        for fact in facts:
            if written >= MAX_RECENT_PER_TURN:
                break
            if str(getattr(fact, "source", "") or "") not in {"rule", "consensus"}:
                continue
            if str(getattr(fact, "principal", "") or "") != str(principal):
                continue
            text = str(getattr(fact, "text", "") or "").strip()
            if not text:
                continue
            # 「希望被叫「X」」這種句法順手更新稱呼段
            marker = "希望被叫「"
            if marker in text:
                call_name = text.split(marker, 1)[1].split("」", 1)[0].strip()
                if call_name:
                    self.userfile.set_call_name(principal, call_name)
                    written += 1
                    continue
            # 同一主體＋同一謂詞只留最新一條（「喜欢猫」→「喜欢狗」是同一個格子）
            if self.userfile.remember(principal, text,
                                      replace_prefix=_fact_prefix(text)):
                written += 1


#: ④ 一輪最多往 `user.md` 的 recent 寫幾條。
MAX_RECENT_PER_TURN = 2


#: `_LIKE` 抓到的謂詞（`fact_rules.py` 那一組）。用來做「同一格子只留最新」的前綴。
_FACT_VERBS = ("喜欢", "討厭", "讨厌", "害怕", "住在", "养了", "養了",
               "擅长", "擅長", "不擅长", "不擅長")


def _fact_prefix(fact_text: str) -> str:
    """回「主體 ＋ 謂詞」前綴（例如 `主人 喜欢`）；抓不到就回空字串（＝不做覆蓋）。"""
    body = str(fact_text or "")
    if " " not in body:
        return ""
    who, _, rest = body.partition(" ")
    for verb in _FACT_VERBS:
        if rest.startswith(verb):
            return f"{who} {verb}"
    return ""
