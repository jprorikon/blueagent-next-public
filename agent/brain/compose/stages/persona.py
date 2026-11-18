"""人格段：人格檔、身分、立場、user.md、委任權限的放行。"""

from __future__ import annotations

import hashlib
import os
import time
from agent.brain.compose.stages.context import TurnContext
from agent.brain.domain.entity import EntityRegistry
from agent.brain.domain.event import Origin
from agent.brain.domain.principal import PrincipalRegistry
from agent.brain.domain.social import SocialGraph
from agent.brain.persona.core import core_hash, prompt_block
from agent.brain.runtime.stance import StanceService
from agent.brain.runtime.inner.timeframe import human_age


def _turn_index(thread, position: int) -> int:
    """這一輪在這條線是**第幾輪**（滾動窗只留最近 40 條，所以要用總數回推）。"""
    history = list(getattr(thread, "transcript", ()) or ())
    total = int(getattr(thread, "turns", 0) or len(history))
    start = max(1, total - len(history) + 1)
    return start + int(position)


#: 說話語言約束（`BLUEAGENT_SPEAK_LANG`）。空＝不約束（Demo 原行為）。
#:
#: 為什麼要有：她的聲線是日文庫，日文庫讀中文台詞會按日文讀音念成亂碼
#: （2026-10-21 實測：同一句 STT 從「我在听」變成「おうが最近」）。文字與聲音要用同一種語言。
#:
#: **放在哪裡很關鍵**（同日實測，同一份 persona、同一批 4 句、temperature 0.8）：
#:   · 規則塞在人格段尾端 ⇒ 4/4 中文，還出現退化輸出（「在。今天？空。」）
#:   · 規則塞在人格段最前 ⇒ 4/4 中文
#:   · 規則掛在**本輪用戶訊息尾端** ⇒ 4/4 日語（`brain/messages.py: TURN_SUFFIX`）
#: ⇒ 這裡只決定「要不要」，實際注入在 messages 層，且**不動 transcript**（原文照存）。
SPEAK_LANGS = ("ja", "zh")


def _split_by_counterpart(rows: list[dict], *, principal: str) -> tuple[list[dict], list[dict]]:
    """把一條線的發言切成「我跟這個人」與「場上其他人」。

    · `user` 發言：說話的人是 principal ⇒ 我的；是別人 ⇒ 別人的。
    · `assistant` 發言：`addressed_to` 是 principal（或沒標）⇒ 我的；標了別人 ⇒ 別人的。
    · 這條線**只有一個人**（沒有別人的發言）⇒ 全部算我的，行為與以前一樣。

    切出來的原因：群聊線把別人跟她的對話也塞進歷史，她會把別人的事當自己的經歷
    （owner 2026-11-03「應該有基於與當前對象交流的歷史上下文」）。
    """
    others = {str(row.get("speaker") or "") for row in rows
              if str(row.get("role") or "") == "user"
              and str(row.get("speaker") or "").strip()
              and str(row.get("speaker")) != principal}
    if not others:
        return list(rows), []
    mine: list[dict] = []
    ambient: list[dict] = []
    for row in rows:
        role = str(row.get("role") or "")
        if role == "user":
            (mine if str(row.get("speaker") or "") == principal else ambient).append(row)
        else:
            addressed = str(row.get("addressed_to") or "")
            (ambient if addressed and addressed != principal else mine).append(row)
    return mine, ambient


class PersonaStage:
    name = "persona"

    def __init__(self, entities: EntityRegistry, principals: PrincipalRegistry,
                 social: SocialGraph, stances: StanceService, *, speak_lang: str = "",
                  soul=None, userfile=None, activity_log=None, affect=None,
                  prompt_stats: dict | None = None,
                  persona_cores: dict | None = None, skills=None, timeline=None,
                  tools=None, activity_store=None, deliveries=None, elsewhere=None) -> None:
        self.entities = entities
        self.principals = principals
        self.social = social
        self.stances = stances
        self.speak_lang = str(speak_lang or "").strip().lower()
        #: 人格檔（`SOUL.md`＋角色檔）。沒有人格檔就不給人格（不再退回 demo 的中文 PERSONAS）。
        #: 貼的位置照現役橋：**每一輪用戶訊息的前面**，不放 system（見 brain/messages.py）。
        self.soul = soul
        #: 每個 principal 一頁 `user.md`（檔案層）。None＝這一層不存在（測試/離線）。
        self.userfile = userfile
        #: 她的行動日誌（private 段會進 prompt，讓她接得上自己剛才在做什麼）。
        self.activity_log = activity_log
        #: 她**此刻**在做的那件事（in-time 現況段；`data/activities/<entity>.json`）。
        #: 日誌是「做完的紀錄」，這是「正在做的」，被問「今なにしてるの」時靠它回答。
        self.activity_store = activity_store
        #: 情緒狀態（語氣注入：心情影響「怎麼說」，不只是「說不說」）。
        self.affect = affect
        #: 固定段指紋（`{"fixed_hash", "fixed_chars", "entity", "at"}`）⇒ `/api/metrics`。
        self._prompt_stats = prompt_stats
        #: 人格 core（`{entity: core}`）——SPIN 第 ① 段編譯出來的結構化人格。
        # ⚠ 不能寫 `persona_cores or {}`：空 dict 是 falsy ⇒ 會換成新 dict，
        # 之後（背景編譯完成）往原 dict 塞 core 就看不到。保留同一個物件。
        self.persona_cores = persona_cores if persona_cores is not None else {}
        #: 工作技能庫（owner 2026-10-23）：只放索引進 prompt，正文由 `skill.view` 取
        self.skills = skills
        #: 工具註冊表：技能索引按渠道過濾（技能要的工具這裡用不了就不列；`runtime/tools/channels.py`）
        self.tools = tools
        #: 時間感（`runtime/timeframe.Timeline`）：bootstrap 晚一步把 archive／heartbeats 補上。
        self.timeline = timeline
        #: 未渡しの依頼（`runtime/inner/pending_delivery.py`）：主人要的交付還沒交 ⇒ 進 `[[todo]]`。
        self.deliveries = deliveries
        #: 跨線餘溫（D2，`ThreadStore.elsewhere`）：她別條活線剛剛聊的幾輪 ⇒ `[[elsewhere]]`。
        self.elsewhere = elsewhere

    def run(self, env, ctx):
        entity = self.entities.get(env.decision.entity)
        principal = self.principals.get(env.decision.principal)
        entity_name = entity.name if entity is not None else env.decision.entity
        principal_name = principal.name if principal is not None else env.decision.principal
        group = env.event.scope.startswith("group:")
        # 這一段的產物都寫進 TurnContext（同一個 env.meta；鍵與寫入者在 context.py 宣告）
        context = TurnContext.of(env)
        persona = context.persona
        persona.group = bool(group)
        soul_block = ""
        if self.soul is not None:
            target = (entity.soul if entity is not None else "") or env.decision.entity
            soul_block = self.soul.block(target)
        system = ""
        if soul_block:
            persona.soul_block = soul_block
        # 人格 core（SPIN 第 ① 段）：**編譯一次、快取**，這裡只貼「肖像＋最強的幾條 if-then」。
        # 沒有 core 就一個字都不多（可回退）。owner 2026-10-22 P0。
        if self.persona_cores:
            core = self.persona_cores.get(env.decision.entity)
            block = prompt_block(core)
            if block:
                persona.core_block = block
                if self._prompt_stats is not None:
                    self._prompt_stats.update({
                        "core_hash": core_hash(core),
                        "core_rules": len((core or {}).get("disposition_rules") or []),
                    })
        system += (f"\n\nいま話しているのは {principal_name}（{env.decision.principal}），"
                   f"あなたは「{entity_name}」。")
        people = list(env.event.participants) or [env.decision.principal]
        if len(people) > 1:
            room = "、".join(self._name(who) for who in people)
            system += f" その場にいる人：{room}。"
            for index, left in enumerate(people):
                for right in people[index + 1:]:
                    line = self.social.relation_line(left, right, self._names())
                    if line:
                        system += f" {line}。"
        if group:
            system += " これはグループ会話：あなたに向けられた発言か、明らかに自分への発言にだけ応じる。"
            system += " その場にいない人・全員に見えない範囲のことは口に出さない。"
        if env.event.origin is not Origin.SELF:
            addressed = env.event.addressed_to if group else ""
            stance = self.stances.render(
                env.decision.entity, env.decision.principal,
                addressed=addressed, names=self._names())
            if stance:
                system += "\n\n" + stance
                persona.stance = stance
        if self.speak_lang in SPEAK_LANGS:
            persona.speak_lang = self.speak_lang
        # 委任開關：`work`＝把這句話當工作指令執行（只有 owner 拿得到，見 server/api/turns.py 的閘）
        mode = str(env.event.payload.get("mode") or "chat").strip().lower()
        persona.mode = mode if mode in {"chat", "work"} else "chat"
        # 身分也由 registry 判（不看 payload 自報）：委任工具只給主人
        principal_obj = self.principals.get(env.decision.principal)
        persona.is_owner = bool(getattr(principal_obj, "owner", False))
        # 情緒 → 語氣（owner 2026-10-22「注入语气」）：只給方向不給數字，很短
        if str(os.environ.get("BLUEAGENT_MOOD_IN_PROMPT") or "1").strip() not in                 {"0", "off", "false", "no"}:
            affect = getattr(self, "affect", None)
            if affect is not None:
                block = self._mood_block(affect, env.decision.entity)
                if block:
                    persona.mood_block = block
        # 她自己的私記（private log）：只有她自己看得到，讓她接得上「剛才在做什麼」
        self._note_blocks(env, persona)
        # 每個 user 一頁（檔案層：identity 手寫／recent 自動，每一輪都進 prompt）
        user_fixed = ""
        if self.userfile is not None:
            fixed_part, recent_part = self.userfile.block_parts(
                env.decision.principal,
                name=getattr(principal_obj, "name", "") or env.decision.principal)
            user_fixed = fixed_part
            joined = "\n\n".join(part for part in (fixed_part, recent_part) if part.strip())
            if joined:
                persona.user_block = ("### この人について（毎回かならず読む・推測で埋めない）\n"
                                          f"{joined}")
        # 委任的放行＝**兩道都過**：① 他是主人（token 那一側的身分）② user.md 的「权限」段放開。
        # 模型自述、payload 自報、人格檔文字都不參與判定 ⇒ 越獄無從下手。
        capabilities: set[str] = set()
        if persona.is_owner and self.userfile is not None:
            if self.userfile.allows(env.decision.principal, "委任"):
                capabilities.add("delegate")
        persona.capabilities = frozenset(capabilities)
        # 語音出口的開關：Discord 這種「本來就是聲音頻道」的來源由源指定要出聲
        if env.event.payload.get("want_voice"):
            context.output.force_voice = True
        if self.skills is not None:
            try:
                # mode／is_owner 都在上面無條件寫過了
                tools = self.tools

                def available(name: str) -> bool:
                    return tools.available(name, mode=persona.mode, is_owner=persona.is_owner,
                                           capabilities=persona.capabilities,
                                           source=str(env.event.source))

                block = self.skills.render_index(mode=persona.mode,
                                                 is_owner=persona.is_owner,
                                                 entity=env.decision.entity,
                                                 tool_available=available if tools is not None else None)
            except Exception:                          # noqa: BLE001 - 技能庫壞了不擋對話
                block = ""
            if block:
                persona.skills_block = block
        for key in ("mode_note", "room_note"):        # room_note：App 房裡她上次看過之後的發言
            value = str(env.event.payload.get(key) or "").strip()
            if value:
                setattr(persona, key, value)
        system += f"\nその場にいる人：{', '.join(people)}。"
        persona.persona = system
        persona.group = group
        persona.ownerless = env.event.origin is Origin.SELF
        persona.entity_name = entity_name
        persona.principal_name = principal_name
        persona.names = self._names()
        context.recall.participants = people
        rows = [
            {"role": turn.role, "text": turn.text, "speaker": turn.speaker,
             "addressed_to": turn.addressed_to,
             # 時間感（owner 2026-10-27）：每輪帶 `at` 與**這條線的第幾輪**，
             # 她才能說「那是 20 輪之前的事」而不是「之前」。
             "at": float(getattr(turn, "at", 0.0) or 0.0),
             "index": _turn_index(env.thread, position)}
            # 條數不再這裡定：`brain/messages.py` 依字符預算從最新往回填
            #（`transcript` 本身已是 deque(maxlen=40)）。
            for position, turn in enumerate(list(env.thread.transcript))
        ]
        self._fill_history(persona, rows, env)
        # 承接段：上一段封片後的摘要（換世代也接得上，見 threadstore / episodes）
        persona.carry_summary = str(getattr(env.thread, "carry_summary", "") or "")
        self._fill_elsewhere(persona, env, people)  # 跨線餘溫（D2，`[[elsewhere]]`）
        self._fill_delivery(persona, env)          # 未渡しの依頼（`[[todo]]`，2026-11-08）
        # 固定段指紋（owner 2026-10-22 第 ③ 步）：人格／身分／這個人的 identity 逐字穩定，
        # 才能觀測「這輪送的是哪一版」，也才吃得到 provider 的 prefix cache。
        # 刻意**不塞進 prompt**（多一行就破壞逐字穩定），只留在 metadata 與 `/api/metrics`。
        if self._prompt_stats is not None:
            canonical = "\n\n".join(part for part in (
                soul_block, system, user_fixed,
                str(persona.stance or ""),
                str(persona.speak_lang or "")) if str(part).strip())
            self._prompt_stats.update({
                "fixed_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12],
                "fixed_chars": len(canonical),
                "fixed_head": canonical.splitlines()[0][:60] if canonical else "",
                "entity": str(env.decision.entity), "at": round(time.time(), 3),
            })
        return None

    @staticmethod
    def _mood_block(affect, entity: str) -> str:
        """「### いまの気分」：低落而原因主人看不見 ⇒ 一句帶過原因，不無端冷淡（情感整理 P4／共同基盤 H8）。"""
        from agent.brain.runtime.inner.mood_cause import main_cause
        from agent.brain.runtime.inner.mood_words import mood_block
        return mood_block(affect.mood(entity), cause=main_cause(affect, entity))

    def _fill_elsewhere(self, persona, env, people) -> None:
        """她別條活線剛剛聊的幾輪（D2，owner 2026-11-17）；無主回合不給，讀壞了不擋這一輪。"""
        if self.elsewhere is None or env.event.origin is Origin.SELF:
            return
        try:
            persona.elsewhere = self.elsewhere(env.decision.entity, current=env.thread.id,
                                               present=people, now=time.time())
        except Exception:                              # noqa: BLE001
            persona.elsewhere = []

    def _fill_delivery(self, persona, env) -> None:
        """主人要的東西她還沒交 ⇒ 這一輪的 prompt 就看得到（`[[todo]]`，2026-11-08）。

        只寫「還開著的」那幾筆；記帳壞了當作沒有這一塊，不擋回話。
        """
        if self.deliveries is None:
            return
        try:
            persona.delivery_block = self.deliveries.render(
                env.decision.entity, principal=str(env.decision.principal))
        except Exception:                              # noqa: BLE001
            persona.delivery_block = ""

    def _names(self) -> dict[str, str]:
        return {principal.id: principal.name for principal in self.principals.all()}

    # ── in-time 現況（owner 2026-11-03）────────────────────────────────────────
    def _fill_history(self, persona, rows: list[dict], env) -> None:
        """歷史按**當前對象**切開，並補上「此刻」的現況（owner 2026-11-03）。

        為什麼要切：群聊線會把別人跟她的對話也塞進歷史，她因此把別人的事當成自己的經歷。
        """
        persona.transcript, persona.ambient = _split_by_counterpart(
            rows, principal=str(env.decision.principal))
        # in-time 現況：她此刻在做什麼（跟「歷史」是兩回事，這是同一條時間軸上的她）
        persona.presence_block = self._presence(env.decision.entity)

    def _presence(self, entity: str) -> str:
        """她此刻在做什麼：正在做的那件事＋進度＋多久之前動的手，外加剛完成的一步。

        為什麼需要：prompt 以前只有「歷史」（跟誰說過什麼）與「私記」（做完的紀錄），
        被問「今なにしてるの？」她只能翻舊帳。`activity_store` 才是**現在**。
        """
        store = self.activity_store
        if store is None:
            return ""
        try:
            activity = store.load(str(entity))
        except Exception:                              # noqa: BLE001 - 現況段壞了不能擋回話
            return ""
        if activity is None:
            return ("### いま\n- いまは何も進めていない（次の思いつきを待っている）。")
        now = time.time()
        lines = ["### いま"]
        status = str(getattr(getattr(activity, "status", ""), "value", "") or "")
        state = {"running": "手を動かしている", "paused": "いったん手を止めている",
                 "ready": "これから始めるところ", "done": "やり終えた",
                 "failed": "うまくいかず止まっている"}.get(status, "手を付けている")
        aged = human_age(now - float(getattr(activity, "updated_at", 0) or now), now=now)
        lines.append(f"- {state}：{activity.title}"
                     f"（{getattr(activity, 'kind', '') or 'task'}・"
                     f"{min(getattr(activity, 'cursor', 0) + 1, getattr(activity, 'total', 0))}"
                     f"/{getattr(activity, 'total', 0)}・{aged}）")
        step = getattr(activity, "current", None)
        if step is not None and not getattr(activity, "done", False):
            lines.append(f"- いまの一手：{step.title}")
        # 直近に終わった一手（あれば「さっき何をしたか」に答えられる）
        finished = [item for item in (getattr(activity, "steps", ()) or ())
                    if str(getattr(getattr(item, "status", ""), "value", "")) == "done"]
        if finished:
            last = finished[-1]
            lines.append(f"- さっき終えた：{last.title}"
                         f"（{human_age(now - float(getattr(last, 'ended_at', 0) or now), now=now)}）")
        lines.append("- これはあなた自身の今の様子。相手が聞いてきたら、この範囲で正直に話す。"
                     "聞かれていないのに延々と報告しない。")
        return "\n".join(lines)

    def _name(self, who: str) -> str:
        principal = self.principals.get(who)
        return principal.name if principal is not None else who

    def _note_blocks(self, env, persona) -> None:
        """她自己的私記（帶「多久之前」）＋時間感（現在時刻／輪次／跳數）。

        兩段都是「她自己的狀態」，一起組；`run()` 只留一句呼叫（函式長度棘輪）。
        """
        now = time.time()
        if self.activity_log is not None:
            # 私記深度（owner 2026-11-02「心情+私記那段加深一點」）：4 → 8 條
            try:
                limit = max(1, int(os.environ.get("BLUEAGENT_SELFLOG_LIMIT") or 8))
            except ValueError:
                limit = 8
            from agent.brain.runtime.activity_log import ActivityLog   # 角色私聊只留「跟誰聊過」
            tail = self.activity_log.entries(env.decision.entity, limit=limit, visibility="private")
            if tail:
                # 帶「多久之前」：不然她會把自己三天前寫的私記當成剛剛（owner 2026-10-27）
                persona.self_log_block = ("### 最近の私記（自分だけが見られる）\n"
                                          + "\n".join(
                                              f"- （{human_age(now - float(row.get('at') or 0), now=now)}）"
                                              f"{ActivityLog.safe_text(row)}"
                                              for row in tail))
        if self.timeline is not None:
            try:
                persona.clock_block = self.timeline.block(env.decision.entity, env.thread)
            except Exception as exc:                        # noqa: BLE001 - 時間感不該擋回話
                print(f"  persona: 時間感段失敗（{type(exc).__name__}: {exc}）", flush=True)
