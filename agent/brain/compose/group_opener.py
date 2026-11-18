"""三人房安静时，她拿起手机后要不要自己开个话题（GROUP-SELF-CHAT §2 ①「定时敲门」的后半段）。

`adapter/app_group_knock.py` 决定她「这次看了手机、房里也安静」之后才来问这里；
这里只做两件事：

  · `compose(entity, lines, quiet_s)`：用她的人格问一句「要不要在房里说点什么」，
    **默认不说**（回 `（なし）` ⇒ 空字串）。说的话要从她此刻的自己自然地进，2 句以内。
  · `record(...)`：真的发出去之后，把这句以 `assistant` 写回她的群聊线
    （`group:<conversation_id>`／`app-group`）——不写的话下一轮别人接话时，她不知道自己说过什么
    （同 `proactive_echo.py` 踩过的坑）。

人格段沿用心跳的 `heartbeat_system`（一人称「私」、称呼「オーナー」的硬提醒都在里面）。
"""

from __future__ import annotations

from agent.brain.compose.heartbeat_compose import heartbeat_system
from agent.brain.domain.event import SourceClass
from agent.brain.domain.ids import ThreadKey
from agent.brain.domain.thread import TurnRecord
from agent.brain.persona.core import prompt_block as _core_prompt_block
from agent.brain.ports import BrainRequest
from agent.brain.router.rules import continuity_window

#: 她决定不说时要回的那个字面值
SILENT = "（なし）"


def opener_prompt(lines: list[tuple[str, str]], quiet_s: float) -> str:
    minutes = int(max(0.0, float(quiet_s)) // 60)
    parts = [f"（スマホを見た。3人の部屋は{minutes}分くらい誰も話していない。）\n" if minutes
             else "（スマホを見た。3人の部屋にはまだ誰も書いていない。）\n"]
    if lines:
        parts.append("部屋の最近のやりとり：\n" + "\n".join(
            f"・{who}：{text[:80]}" for who, text in lines[-8:]) + "\n")
    parts.append(
        "自分から部屋に一言書くかどうか決める。**たいていは何も書かない**——"
        "話したいことが本当にあるときだけ書く。前のやりとりを蒸し返すだけ、"
        "あいさつだけ、誰かに無理に話を振るだけなら書かない。\n"
        "書くなら、いまの自分（〜してた／ふと思った）から自然に入って、2文まで、質問は最大ひとつ。\n"
        f"書かないときは「{SILENT}」とだけ出力する。出力はその一言だけ。"
    )
    return "".join(parts)


def _clean(text: str) -> str:
    line = str(text or "").strip().splitlines()[0].strip() if str(text or "").strip() else ""
    line = line.strip("「」\"'")
    if not line or line.startswith(SILENT) or line in {"なし", "（無）", "(なし)", "…", "..."}:
        return ""
    return line


class GroupOpener:
    """`compose`／`record` 两个端口，交给 `build_app_chat_adapters(opener=…)`。"""

    def __init__(self, *, brain, threads, persona_cores=None, speak_lang: str = "", log=None) -> None:
        self.brain = brain
        self.threads = threads
        self.persona_cores = persona_cores or {}
        self.speak_lang = str(speak_lang or "")
        self.log = log or (lambda _message: None)

    def compose(self, entity: str, lines: list[tuple[str, str]], quiet_s: float) -> str:
        system = heartbeat_system("", _core_prompt_block(self.persona_cores.get(str(entity))))
        request = BrainRequest(
            thread_id=f"group-opener:{entity}", entity=str(entity), principal="owner",
            text=opener_prompt(lines, quiet_s), system=system, mode="chat",
            meta={"speak_lang": self.speak_lang})
        text = ""
        try:
            for kind, payload in self.brain.run_turn(request):
                if str(kind) == "done":
                    text = str(payload.get("text") or "")
        except Exception as exc:                       # noqa: BLE001 - 合不出来就当她没开口
            self.log(f"group opener[{entity}]: 生成失败（{type(exc).__name__}）")
            return ""
        return self._grounded(entity, _clean(text), lines, quiet_s)

    def _grounded(self, entity: str, text: str, lines, quiet_s: float) -> str:
        """出口閘的規則層（common-ground 第 5 步）：房裡**最近沒有可見的話**卻用「例の／さっきの」指 ⇒ 不發。

        群裡大家看得到的就是房裡那幾則（`lines`），所以「最近有可見的對話」＝房裡有話、而且安靜不到 30 分鐘。
        這裡不重寫（開場本來就「たいていは何も書かない」），懸空就當她沒開口。
        """
        if not text:
            return ""
        from agent.brain.compose.heartbeat_compose import VISIBLE_FRESH_S
        from agent.brain.runtime.inner.premise import PremiseCheck

        fresh = bool(lines) and float(quiet_s or 0.0) <= VISIBLE_FRESH_S
        result = PremiseCheck(critic=None, log=self.log).review(text, fresh=fresh)
        if not result["text"]:
            self.log(f"group opener[{entity}]: 指代懸空 ⇒ 不開場（{result['missing']}）")
        return str(result["text"])

    def record(self, entity: str, conversation_id: str, text: str, at: float, participants=()) -> bool:
        body, who = str(text or "").strip(), str(entity or "").strip()
        if not body or not who or self.threads is None:
            return False
        key = ThreadKey(scope="group:" + str(conversation_id), entity=who, scene="app-group",
                        purpose="chat")
        try:
            # 与正常的 App 群聊回合同一个连续窗／车道（INTERACTIVE、15 分钟），下一轮才会接在同一代线上
            thread = self.threads.attach(key, now=float(at),
                                         window=continuity_window(SourceClass.INTERACTIVE),
                                         lane=SourceClass.INTERACTIVE).thread
            with thread.lock:
                thread.participants.update(str(item) for item in (participants or ()) if str(item))
                thread.record(TurnRecord(turn_id=f"t_opener_{int(float(at) * 1000)}",
                                         role="assistant", text=body, source="app-group-opener",
                                         at=float(at), speaker=who, addressed_to="group",
                                         engage="opener"))
                thread.touch(float(at))
        except Exception as exc:                       # noqa: BLE001 - 记不上不能影响已发出的消息
            self.log(f"group opener[{entity}]: 写回群聊线失败（{type(exc).__name__}）")
            return False
        return True


__all__ = ["GroupOpener", "SILENT", "opener_prompt"]
