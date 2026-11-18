"""心跳台詞：她自己組織（不是模板挑一句），接地在她真的在做／聊過／想過的事上。

從 `bootstrap.build()` 拆出來（2026-10-25 結構 review 第 3 階段）：以前是 build 裡一個
100 行的閉包，抓了十個區域變數；現在是一個工廠，依賴全部從參數進來。
"""

from __future__ import annotations

from agent.brain.persona.core import prompt_block as _core_prompt_block
from agent.brain.ports import BrainRequest
# 來源 → 端的人話：與 `[[clock]]` 的換端提醒共用一張表
from agent.brain.runtime.inner.timeframe import channel_label as _channel
# 心情的話與原因：詞表在 `mood_words`，原因在 `mood_cause`（情感整理 P7）
from agent.brain.runtime.inner.mood_cause import main_cause

_WEEKDAYS = ("月", "火", "水", "木", "金", "土", "日")
#: 生物鐘狀態 → 她自己的話（`runtime/inner/circadian.py` 的四段）
_BODY = {"awake": "もう起きている", "waking": "起きたばかり", "winding": "そろそろ寝る時間",
         "asleep": "本当は寝ている時間"}


def _part_of_day(hour: int) -> str:
    return ("深夜" if hour < 5 else "朝" if hour < 10 else "午前" if hour < 12 else
            "昼" if hour < 15 else "午後" if hour < 17 else "夕方" if hour < 19 else
            "夜" if hour < 23 else "深夜")


def now_line(entity: str, proactive=None, *, now: float | None = None) -> str:
    """組句的第一行：**現在幾點、她醒著沒**（DIAGNOSIS 2026-11-10 A1）。

    閘（`ProactiveGate.allows`）算過安靜時段與生物鐘，但以前判完就丟——寫字的這一側只拿到
    「N 分前」和「昨晚聊到哪」⇒ 上午十點還在說「夜が冷える」「まだ起きているのかしら」。
    生物鐘用閘手上那一份（`proactive.circadian`），兩邊看同一個鐘。
    """
    import time

    at = time.time() if now is None else float(now)
    moment = time.localtime(at)
    line = (f"いま：{time.strftime('%m月%d日', moment)}（{_WEEKDAYS[moment.tm_wday]}）"
            f"{time.strftime('%H:%M', moment)}・{_part_of_day(moment.tm_hour)}")
    clocks = getattr(proactive, "circadian", None) or {}
    clock_of = clocks.get(str(entity)) if hasattr(clocks, "get") else None
    if clock_of is not None:
        try:
            body = _BODY.get(str(clock_of.state(at)), "")
        except Exception:                                  # noqa: BLE001 - 鐘壞了就只報時刻
            body = ""
        if body:
            line += f"（自分は{body}）"
    return line
from agent.brain.runtime.inner.mood_words import LOW_MOODS, mood_note  # noqa: F401


def heartbeat_prompt(note: str, situation: list[str], ground: list[str]) -> str:
    """心跳台詞的 prompt（owner 2026-10-24：「這次的搭話內容也有點唐突」）。

    以前寫「前置きも説明も書かない」⇒ 她只能從半句開始，聽起來沒頭沒腦。
    現在要求**自然な入り方**（先說自己此刻在做的／想到的），並禁止拿主人沒說過的事情
    當前提（她曾用 TRPG 播報裡的「足跡」去搭話）。
    """
    parts: list[str] = [str(note or "")]
    if situation:
        parts.append("状況：\n" + "\n".join(f"・{line}" for line in situation) + "\n")
    if ground:
        parts.append("\n".join(ground) + "\n")
    parts.append(
        "自分から主人に一言だけ話しかけたい。"
        "**自然な入り方**で書く：まず今の自分（〜してた／ふと思った）を一言置いてから用件。"
        "半句から始めない、主人が言っていないことを前提にしない、知らない出来事を持ち出さない。"
        "**状況の「いま」の時刻に合わせて書く**（朝に夜のあいさつをしない。要約の「今夜／さっき」は"
        "その時点での言い方で、いまのことではない）。"
        "全体で2文まで、質問は最大ひとつ。出力はその一言だけ。"
    )
    return "".join(parts)


def intent_lines(intent) -> list[str]:
    """獨白的 talk 動機 → 組句 prompt 的 ground 行（沒有就空）。

    寫明「主人は知らない」：獨白看得到世界層、私人活動、到期預約，主人一句都沒看過。
    沒有這句，模型會把獨白裡的東西當成雙方都知道的前提（「あれ、どうだった？」）。
    """
    if not isinstance(intent, dict):
        return []
    lines: list[str] = []
    text = str(intent.get("text") or "").strip()
    if text:
        line = ("さっき心の中で思っていたこと（**オーナーはこれを読んでいない**。"
                "そのまま送らない。ここに出てくる事を話すなら、まず何の話かを一言で伝えてから）："
                + text[:200])
        why = str(intent.get("why") or "").strip()
        if why:
            line += f"\n話しかけたくなった理由：{why[:40]}"
        lines.append(line)
    fix = str(intent.get("fix") or "").strip()
    if fix:
        # 出口閘打回來的（`runtime/inner/premise.py`）：上一版主人看不懂，缺的是這個
        lines.append("前に書いた一言は、オーナーには通じなかった（" + fix[:60] + "）。"
                     "何の話かが分かるように、前提から書き直す。「例の」「さっきの」で指さない")
    return lines




def _ago(at: float, now: float) -> str:
    gap = max(0.0, float(now) - float(at or 0.0))
    if not at:
        return ""
    if gap < 90:
        return "たったいま"
    if gap < 3600:
        return f"{int(gap // 60)}分前"
    if gap < 86400:
        return f"{int(gap // 3600)}時間前"
    return f"{int(gap // 86400)}日前"


def _unanswered(proactive, entity: str, principal: str = "owner") -> list[float]:
    try:
        return list(proactive.unanswered(str(entity), principal=str(principal or "owner")) or [])
    except Exception:                                      # noqa: BLE001 - 舊閘／壞了 ⇒ 當沒有
        return []


#: 她上一條主動消息還沒回時，組句與獨白都看到這一句（common-ground 第 4 步）
#: owner 2026-11-09：還沒回也可以再說，但**不能重複**，要意識到已經說過
_AGAIN = ("もう一度話しかけるなら、前の一言を送ったのを分かっている言い方にする"
          "（同じ用件を繰り返さない・言い換えもしない。別の話なら別の話として）")
UNANSWERED_NOTE = ("オーナーはまだ返していない。読んだとは限らない。"
                   "読んだ前提で話さない（「さっきの見た？」と言わない）、返事を催促しない。" + _AGAIN)
#: 走 App Server 會話送出、平台說「還沒讀」（§7.4）
UNREAD_NOTE = ("オーナーはまだ読んでいない（既読がついていない）。"
               "読んだ前提で話さない（「さっきの見た？」と言わない）、返事を催促しない。" + _AGAIN)
#: 平台說「讀了、但沒回」
READ_NOTE = ("オーナーは読んだが、返事はまだ。既読のまま返さないのは普通のこと。"
             "返事を催促しない。" + _AGAIN)


def pending_note(proactive, entity: str, principal: str = "owner") -> str:
    """她最後一條主動消息還沒回 ⇒ 該給模型的那一句（讀了／沒讀／不知道）；回了 ⇒ 空。"""
    if proactive is None or not _unanswered(proactive, entity, principal):
        return ""
    try:
        state = str(proactive.delivery_state(str(entity), principal=str(principal or "owner")) or "")
    except Exception:                                      # noqa: BLE001 - 舊閘沒有這支
        state = ""
    return {"unread": UNREAD_NOTE, "read": READ_NOTE}.get(state, UNANSWERED_NOTE)


def said_lines(proactive, entity: str, principal: str = "owner") -> list[str]:
    """心跳組句的「最近もう口にしたこと」＋（還沒回時）那一條的提醒。沒有就空。"""
    if proactive is None:
        return []
    try:
        said = list(proactive.recent_texts(str(entity), limit=3) or [])
    except Exception:                                      # noqa: BLE001
        said = []
    lines: list[str] = []
    if said:
        lines.append("最近もう口にしたこと（繰り返さない）：" + "｜".join(row[:30] for row in said))
    note = pending_note(proactive, entity, principal) if said else ""
    if note:
        lines.append(f"自分から最後に送った一言「{said[-1][:40]}」：" + note)
    return lines


def talk_digest(owner_rows, own_last: str = "", *, now: float, limit: int = 3,
                unanswered=False) -> str:
    """給獨白的「最近の会話」：主人最近真的說過的話（帶多久前／哪個端）＋她最後主動說的一句。

    以前獨白這一欄寫死空字串（`Heartbeat._monologue_facts`）⇒ 她看不到對話，
    卻在決定要不要找主人說話（docs/PLAN-common-ground-20261109.md H1）。
    端寫出來，是因為主人在 Discord 說的事她在 App 接，主人在 App 裡翻不到（H3）。
    """
    parts: list[str] = []
    for row in list(owner_rows or ())[-max(1, int(limit)):]:
        text = str((row or {}).get("text") or "").strip()
        if not text:
            continue
        tags = "・".join(tag for tag in (_ago(row.get("at") or 0.0, now),
                                         _channel(row.get("source") or "")) if tag)
        parts.append(f"オーナー{f'（{tags}）' if tags else ''}「{text[:60]}」")
    own = str(own_last or "").strip()
    if own:
        # `unanswered`：True（舊呼叫）＝不知道讀了沒；字串＝`pending_note()` 給的那一句
        note = unanswered if isinstance(unanswered, str) else (UNANSWERED_NOTE if unanswered else "")
        parts.append(f"自分が最後に自分から送った一言「{own[:60]}」" + (f"（{note}）" if note else ""))
    return "；".join(parts)


def make_recent_talk(*, threads, proactive, principals, clock=None):
    """回 `recent_talk(entity) -> str`（給心跳的獨白 facts；見 `talk_digest`）。"""
    import time

    now = clock or time.time
    owners = tuple(p.id for p in principals.all() if getattr(p, "owner", False)) or ("owner",)

    def recent_talk(entity: str) -> str:
        rows = threads.recent_owner_lines(str(entity), 3, owners=owners) if threads is not None else []
        said = proactive.recent_texts(str(entity), limit=1) if proactive is not None else []
        return talk_digest(rows, said[-1] if said else "", now=now(),
                           unanswered=pending_note(proactive, entity))

    return recent_talk


#: 「最近有可見的對話」的窗（秒）：這段時間內的話，主人那邊還看得到、指得到
VISIBLE_FRESH_S = 30 * 60


def make_visible(threads, *, principal: str = "owner", limit: int = 6, clock=None):
    """回 `visible(entity) -> {"lines", "fresh"}`：主人↔她那條（非群聊）最近的幾則，給出口閘看。

    跟 `proactive_echo` 挑的是同一種線：只有她和他、不是群聊——主動消息落在那裡，主人也在那裡看。
    """
    import time

    now = clock or time.time

    def visible(entity: str) -> dict:
        rows = []
        for thread in list(threads.threads()) if threads is not None else []:
            key = getattr(thread, "key", None)
            if str(getattr(key, "entity", "")) != str(entity):
                continue
            if str(getattr(key, "scope", "") or "").startswith("group:"):
                continue
            people = {str(item) for item in (getattr(thread, "participants", ()) or ())}
            if people - {str(principal), str(entity)}:
                continue
            rows.extend(list(getattr(thread, "transcript", ()) or ()))
        rows.sort(key=lambda turn: float(getattr(turn, "at", 0.0) or 0.0))
        rows = [turn for turn in rows if str(getattr(turn, "text", "") or "").strip()][-limit:]
        at = float(now())
        lines = [("オーナー" if str(getattr(turn, "role", "")) == "user" else "相手")
                 + f"（{_ago(getattr(turn, 'at', 0.0), at) or '?'}）：" + str(turn.text)[:80]
                 for turn in rows]
        fresh = bool(rows) and at - float(getattr(rows[-1], "at", 0.0) or 0.0) <= VISIBLE_FRESH_S
        return {"lines": lines, "fresh": fresh}

    return visible


def heartbeat_extras(*, threads, proactive, principals, log=None) -> dict:
    """心跳的三樣附件（給 `build_heartbeats(**extras)`）：最近の会話、出口閘、主人看得到的。"""
    import os

    from agent.brain.persona.core import http_call
    from agent.brain.runtime.inner.premise import PremiseCheck

    critic = http_call(dict(os.environ), max_tokens=200, disable_thinking=True, role="premise")
    return {"recent_talk": make_recent_talk(threads=threads, proactive=proactive,
                                            principals=principals),
            "premise": PremiseCheck(critic=critic, log=log),
            "visible": make_visible(threads)}


def heartbeat_system(soul_block: str = "", core_block: str = "") -> str:
    """主動訊息用的人格區塊（owner 2026-10-24）。

    為什麼要拉成純函式：主動訊息那條路以前**完全沒有** system 提示詞，靠模板硬湊；
    實測她因此開始用「俺」自稱（人格檔明明寫著一人稱是「私」）。這裡把 soul 檔的
    人格、persona core 的肖像／if-then，再加一句一人稱的硬性提醒一起給。
    """
    parts: list[str] = []
    for block in (soul_block, core_block):
        text = str(block or "").strip()
        if text:
            parts.append(text)
    # 硬性提醒：不管人格檔有沒有寫，這一行一定在（最短、最不容易被忽略）
    parts.append("一人称は「私」。相手は「オーナー」。この二つは絶対に変えない。")
    return "\n\n".join(parts)


def owner_words(dialogue, principals) -> list[str]:
    """從一串輪次裡只挑**主人真的說過的話**（組句的「主人が最後に言っていたこと」）。

    退路（活線 transcript／raw）也一律過濾：以前只在「沒接 recent_owner_lines」時才過濾 ⇒
    它回空時，角色互聊的「……う」被寫成主人說的（DIAGNOSIS 2026-11-10 §2.3）。
    桌（TRPG）的輪次在資料形狀上是 role=user／speaker=owner，其實是桌的 prompt，也不算。
    """
    listing = getattr(principals, "all", None)
    owners = ({p.id for p in listing() if getattr(p, "owner", False)} if callable(listing) else set()) \
        | {"owner", str(principals.default())}
    words: list[str] = []
    for row in dialogue or ():
        speaker = str(row.get("speaker") or row.get("role") or "")
        text = str(row.get("text") or "").strip()
        if not text or str(row.get("source") or "").lower().startswith(("trpg", "table")):
            continue
        if str(row.get("role") or "user") != "user" or speaker not in owners:
            continue
        words.append(text[:60])
    return words


def make_heartbeat_composer(*, brain, souls, persona_cores, activity_log, affect, prethinker,
                            proactive, raw_log, principals, speak_lang, latest_window,
                            recent_dialogue=None, recent_owner_lines=None, deliveries=None):
    """回一個 `compose(entity, mood) -> str`（`Heartbeat` 閘放行之後才呼叫）。

    `latest_window(entity)`：最近一條**主人聽得的**窗口摘要（披露規則在呼叫端那支裡）。
    `recent_dialogue(entity, limit)`：最近一條活線的 transcript 尾巴（可選；沒有就退回 raw）。
    `recent_owner_lines(entity, limit)`：**跨線**取主人真的說過的話（2026-11-08）。
    `deliveries`：主人要她交付、她還沒交的東西（`runtime/inner/pending_delivery.py`）。
    """

    def compose(entity: str, mood: dict, due=(), intent=None) -> str:
        """心跳台詞**由她自己組織**（owner 2026-10-22 D）：不是模板挑一句。

        只在閘放行之後才會被呼叫（`Heartbeat.tick` 先過閘再合），所以一天最多十幾次。
        失敗／空字回空字串 ⇒ `Heartbeat` 自動退回模板，不會因此不說話。

        `due`：這一跳到期的預約（`runtime/inner/reminders.py`，owner 2026-10-27「幾跳之後提某件事」）
        ——當燃料放進 prompt，要不要提、怎麼提由她自己決定。`intent`：見 `intent_lines()`。
        """
        note = mood_note(str(mood.get("label") or "").strip(), main_cause(affect, entity))
        # **接地**（owner 2026-10-23）：不要凭空撒嬌——用她自己在做的事／剛聊過的事當燃料。
        ground: list[str] = []
        ground.extend(intent_lines(intent))
        if due:
            # 「幾跳之後要提的事」（owner 2026-10-27）：只在到期那一跳出現一次
            ground.append("前に自分へ約束して、いま出すことにしていた件：" + "；".join(
                str(item)[:60] for item in due if str(item).strip()))
        if activity_log is not None:
            try:
                tail = activity_log.private_tail(str(entity), limit=3)
            except Exception:                              # noqa: BLE001
                tail = []
            # 只留「她自己的事」；TRPG 播報那種第三人稱敘事拿來搭話會很突兀
            #（實測 12:12 那句「その足跡、私も追いかけてたよ？」就是這樣來的）。
            skip = ("足跡", "ターン", "GM", "p1", "p2", "p3", "p4", "席")
            clean = [str(row)[:40] for row in tail
                     if not any(mark in str(row) for mark in skip)]
            if clean:
                ground.append("最近やっていること：" + "；".join(clean))
        window = ""
        try:
            window = latest_window(str(entity))
        except Exception:                                  # noqa: BLE001
            window = ""
        if window:
            ground.append("最近話したこと：" + window[:160])
        planned = ""
        if prethinker is not None:
            try:
                # 心跳只發給主人 ⇒ 預想也要主人聽得（客人那段的預想不拿）
                planned = prethinker.peek(str(entity), present={principals.default()})
            except Exception:                              # noqa: BLE001
                planned = ""
        if planned:
            ground.append("さっき自分で考えていたこと（オーナーは知らない）：" + planned[:120])
        ground.extend(said_lines(proactive, str(entity), str(principals.default())))
        # 未渡しの依頼（2026-11-08）：主人要的東西還沒交 ⇒ 先交（或說明交不了），
        # 不要把自己的欠帳說成對方的欠帳（實測 aster 連五次催「オーナーの番」）。
        owed = ""
        if deliveries is not None:
            try:
                owed = deliveries.render(str(entity), principal=str(principals.default()))
            except Exception:                              # noqa: BLE001
                owed = ""
        if owed:
            ground.append("主人に頼まれて、まだ渡していないもの（先に渡す。渡せないなら理由を一言）：\n"
                          + owed)
        # ⚠ 2026-10-24：這裡以前**沒帶 `system`**（人格是 PersonaStage 才貼進去的），
        # 於是主動訊息是在「沒有角色設定」的情況下生成的——實測她開始用「俺」自稱、
        # 語氣也變了。這條路要自己把人格貼上：soul 檔 ＋ persona core（同一個來源）。
        system = heartbeat_system(
            souls.block(str(entity)) if souls is not None else "",
            _core_prompt_block((persona_cores or {}).get(str(entity))),
        )
        # **為什麼現在開口**（owner 2026-10-24：「這次的搭話內容也有點唐突」）
        # 以前的 prompt 寫「前置きも説明も書かない」⇒ 她只能從半句話開始，聽起來像沒頭沒腦。
        # 現在給三個具體依據：多久沒說話、此刻在做什麼、上一次跟主人講到哪。
        situation: list[str] = [now_line(str(entity), proactive)]
        try:
            gap = affect.idle_s(str(entity)) if affect is not None else 0.0
        except Exception:                                  # noqa: BLE001
            gap = 0.0
        if gap:
            minutes = int(gap // 60)
            situation.append(f"主人と最後に話してから約{minutes}分")
        now_doing = ""
        if activity_log is not None:
            try:
                doing = activity_log.public_tail(str(entity), limit=1)
            except Exception:                              # noqa: BLE001
                doing = []
            if doing:
                now_doing = str(doing[-1]).split("：")[-1][:60]
                situation.append("いまやっていたこと：" + now_doing)
        # 2026-11-08：先**跨線**找主人真的說過的話。`recent_dialogue` 取的是「最近一條活線」，
        # 那條可能是角色↔角色的線（實測：aster 的最近活線是跟ミラ互刷「……う」）⇒ 主人的話
        # 整段被過濾掉，她只剩自己上一句可以換詞（連五次「オーナーの番」）。
        dialogue: list[dict] = []
        if recent_owner_lines is not None:
            try:
                dialogue = recent_owner_lines(str(entity), 3) or []
            except Exception:                              # noqa: BLE001
                dialogue = []
        # 2026-11-01：當前活線的 transcript（窗口全量），raw 只當兜底。
        if not dialogue and recent_dialogue is not None:
            try:
                dialogue = recent_dialogue(str(entity), 12) or []
            except Exception:                              # noqa: BLE001
                dialogue = []
        if not dialogue and raw_log is not None:
            try:
                dialogue = raw_log.recent(str(entity), limit=12)
            except Exception:                              # noqa: BLE001
                dialogue = []
        last_talk = owner_words(dialogue, principals)[-3:]
        if last_talk:
            situation.append("主人が最後に言っていたこと：" + "｜".join(last_talk))
        request = BrainRequest(
            thread_id=f"heartbeat:{entity}", entity=str(entity), principal="owner",
            text=heartbeat_prompt(note, situation, ground), system=system, mode="chat",
            meta={"speak_lang": speak_lang},
        )
        text = ""
        try:
            for kind, payload in brain.run_turn(request):
                if str(kind) == "done":
                    text = str(payload.get("text") or "").strip()
        except Exception as exc:                       # noqa: BLE001 - 合不出來就退回模板
            print(f"  heartbeat[{entity}]: 自己組織台詞失敗（{type(exc).__name__}）", flush=True)
            return ""
        return text.splitlines()[0].strip().strip("「」\"'") if text else ""

    return compose


__all__ = ["LOW_MOODS", "mood_note", "READ_NOTE", "UNANSWERED_NOTE", "UNREAD_NOTE", "VISIBLE_FRESH_S", "heartbeat_extras",
           "heartbeat_prompt", "heartbeat_system", "intent_lines", "make_heartbeat_composer",
           "make_recent_talk", "make_visible", "pending_note", "said_lines", "talk_digest"]
