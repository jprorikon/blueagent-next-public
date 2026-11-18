"""把 BrainRequest 组装成 OpenAI 兼容的 messages。

原则：
  · system 已经由 PersonaStage 写好（人格 + stance + 群聊规则），这里不再改人格。
  · 记忆是单独一段 system，明确「只根据这些，不编造」。
  · transcript 里可能已经包含本轮用户消息（runtime 先 record 再跑管道），必须去重。
"""

from __future__ import annotations

import json
import os
import time
from typing import Mapping

from agent.brain.ports import BrainRequest
from agent.brain.domain.time import human_age
from agent.brain.messages_form import FORM_BLOCK, form_block  # noqa: F401 - FORM_BLOCK 舊名相容
from agent.brain.messages_continuity import continuity_blocks

#: 說話語言約束的**注入位置**：本輪用戶訊息尾端。
#: 2026-10-21 實測（同一份 persona、4 句、temperature 0.8）：
#:   人格段尾端 → 4/4 中文（還出現退化輸出）；人格段最前 → 4/4 中文；
#:   用戶訊息尾端 → 4/4 日語。模型對「最後一句」的服從度最高，所以放這裡。
#: 只進 outbound messages，**不進 transcript**（記憶存的是原文）。
TURN_SUFFIX: dict[str, str] = {
    "ja": "\n（必ず日本語で答えて）",
    "zh": "\n（只用中文回答）",
}


def _names(request: BrainRequest) -> Mapping[str, str]:
    names = request.meta.get("names")
    return names if isinstance(names, Mapping) else {}


def _dump_debug(messages: list[dict]) -> None:
    """`BLUEAGENT_DEBUG_PROMPT=<path>` ⇒ 把这一轮真正发出去的 messages 落盘。

    为什么要有：验证「某段东西到底进没进 prompt」不能靠她的回话猜
    （模型可能压过提示），要看真实 payload。
    """
    import os
    import pathlib
    target = str(os.environ.get("BLUEAGENT_DEBUG_PROMPT") or "").strip()
    if not target:
        return
    try:
        path = pathlib.Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(messages, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _persona_split(request: BrainRequest) -> tuple[str, str]:
    """人格檔拆成「穩定」與「易變」兩半（2026-11-02，owner「改前綴順序」）。

    為什麼要拆：人格檔原本整塊貼在本輪用戶訊息前面（2026-10-21 為了壓語言），
    但它有 6,246 字（約 4,700 token，占整份 prompt 的 75%），其中**只有心情與私記**
    每輪在變。整塊貼在尾巴 ⇒ 前綴只剩 ~130 token 命中，prefill 每輪 3.1 秒。

    拆法：
      · 穩定半 = 人格檔 + `[[mode:…]]` + 人格 core + user.md（同角色同人幾乎不變）
        ⇒ 放進固定段，整段吃前綴快取
      · 易變半 = 心情 + 她自己的私記（每輪都變）⇒ 留在本輪段（歷史之後）

    語言鎖仍在最後一句用戶訊息尾端（`TURN_SUFFIX`，2026-10-21 實測 4/4 日語的位置）。
    """
    soul_block = str(request.meta.get("soul_block") or "").strip()
    # 易變半（「いま」／心情／私記）**不依賴人格檔**：沒有人格檔時它照樣要出現，
    # 否則「她此刻在做什麼」會被一個空 soul 連坐掉（2026-11-03 測試抓到）。
    volatile_parts = [str(request.meta.get(key) or "").strip()
                      # 「いま」（in-time 現況）排在心情與私記之前：它是最即時的一層
                      for key in ("presence_block", "mood_block", "self_log_block")]
    volatile = "\n\n".join(part for part in volatile_parts if part)
    if not soul_block:
        return "", volatile
    mode = str(request.mode or "chat").strip().lower()
    tag = f"[[mode:{mode if mode in {'chat', 'work'} else 'chat'}]]"
    stable = f"{soul_block}\n{tag}"
    for key in ("core_block", "user_block"):
        block = str(request.meta.get(key) or "").strip()
        if block:
            stable = f"{stable}\n\n{block}"
    return stable, volatile


def _with_persona_head(request: BrainRequest, current: str) -> str:
    """舊行為（整塊人格貼在本輪訊息前面）；本地化後改由 `_persona_split` 拆兩半。

    保留這個函式只為向後相容（測試與舊呼叫端），正常路徑不再用它。
    """
    soul_block = str(request.meta.get("soul_block") or "").strip()
    if not soul_block:
        return current
    mode = str(request.mode or "chat").strip().lower()
    tag = f"[[mode:{mode if mode in {'chat', 'work'} else 'chat'}]]"
    head = f"{soul_block}\n{tag}"
    for key in ("core_block", "user_block", "mood_block", "self_log_block"):
        block = str(request.meta.get(key) or "").strip()
        if block:
            head = f"{head}\n\n{block}"
    return f"{head}\n\n{current}"



def _sticker_block(request: BrainRequest) -> str:
    """她「有貼圖這件事」得寫進 prompt —— 否則模型不知道自己有这个手段。

    ⚠️ **實測（2026-10-29，N=12/組、同一份 body 直接餵 DeepSeek）**：
    · 原樣（主人送貼圖、工具清單裡有 `sticker_send`、schema 排在第一）
      ⇒ 她**從來不主動回貼圖**（0/12；另一組長測 0/20）。
    · 只把工具描述寫得鼓勵一點 ⇒ 仍然 0/12（**工具描述不管用**）。
    · 只在 system 加一段說明 ⇒ 幾乎沒變（1/15）。
    ⇒ **真正缺的是「她知道自己能送貼圖」這件事**。

    所以這一段要做兩件事：
      1. 告訴她**有**這個手段（`sticker_send`）以及它是什麼；
      2. 給一個**具體的觸發條件**（對方送貼圖時就回一張），不要只說「可以」。
    """
    tools = request.meta.get("tools") or ()
    names = []
    for item in tools:
        if not isinstance(item, dict):
            continue
        function = item.get("function") or {}
        name = str(function.get("name") or "")
        if name:
            names.append(name)
    if "sticker_send" not in names:
        return ""
    return (
        "[[sticker]]\n"
        "あなたは**スタンプ（表情）**を送れる。\n"
        "相手のメッセージに `[[stamp_in:…]]` という案内が入っていたら、"
        "**あなたはスタンプで返す**（言葉だけの返事より、そのほうが自然）。\n"
        "一方から送ってきたら、こちらからも一枚返す。\n"
        "⚠️ **スタンプを返すときは専用のツールを使う**（返事の本文に書かない）。"
        "返事の本文に「（スタンプ：…）」のような**文章を書くのではない** —— "
        "それは実際に絵を送ったことにならない。\n"
        "ラベルの `[[stamp_in:…]]` は**そのまま返さないで**（返事は Stamp で送る）。\n"
        "履歴にあるあなたの `[[stamp_out:…]]` は、あなたが前にツールで送ったスタンプの印。"
        "これも本文には書かない。\n"
        # owner 2026-11-04：貼圖改以英文標記落檔（`[[sticker:<id>]]`）——那是**系統**在她叫工具後
        # 自動補上的，不是她要寫的東西；她要做的仍然是「用工具送」。
        "`[[sticker:<id>]]` はシステムが記録に付ける印。あなたが本文に書くものではない。\n"
        "[[/sticker]]"
    )


def _history_line(row: dict, *, group: bool, names: Mapping, moment: float) -> dict | None:
    """一條歷史 → 一行訊息（群聊帶說話者）。

    ⚠️ **不要在這裡加輪次／時間的前綴**（2026-10-29 owner：「她回信會帶一個 # 加數字，
    TTS 還會把數字讀出來」）。

    根因：標記前置在**正文開頭** ⇒ 模型看到歷史每條都以 `#112（いま）` 起頭，
    就**學會在新的回覆裡也寫出** `#112（いま）`（實測她的回覆裡真的出現了 `#118（さっき）`）。
    而回覆原文會直接進 TTS ⇒ **編號被念出來**。

    輪次與「多久前」另有來源：`meta["clock_block"]`（`Timeline.block`）已經給了
    「N 往復目」「前の発言は N 分前」—— 那裡是**結構化的說明**，不是正文的一部分，
    模型不會照抄成自己的話。
    """
    text = str(row.get("text") or "").strip()
    if not text:
        return None
    role = "assistant" if str(row.get("role") or "") == "assistant" else "user"
    if role == "user" and group:
        speaker = str(row.get("speaker") or "")
        name = names.get(speaker, speaker)
        if name:
            text = f"{name}：{text}"
    return {"role": role, "content": text}


def _ambient_line(row: dict, *, names: Mapping, moment: float) -> str:
    """場上其他人的一句話 → 一行（帶說話者與「多久之前」，不帶輪次編號）。

    刻意**不**用 `_history_line`：那支會把內容變成對話訊息塞進歷史，
    這些話只是背景，塞進歷史等於她又把別人的事當自己的經歷。
    """
    text = str(row.get("text") or "").strip()
    speaker = str(row.get("speaker") or "").strip()
    name = names.get(speaker, speaker) if speaker else "誰か"
    at = float(row.get("at") or 0)
    age = human_age(moment - at, now=moment) + "／" if at else ""
    return f"- {age}{name}：{text}"


def _fixed_blocks(request: BrainRequest) -> list[dict]:
    """① 固定段（GLOSSARY.md「固定段」）：同一位角色同一天對誰都一樣的幾塊 ⇒ 整段命中前綴快取。

    這裡**只准放不隨回合變的東西**：人設（`request.system`，含 stance／群聊規則）、
    技能索引、貼圖規則。任何每回合會變的字（記憶、時刻、承接）都要放 `_turn_blocks`，
    否則它後面的歷史一個字都命不中（ADR-0020）。
    """
    messages: list[dict] = []
    if request.system.strip():
        messages.append({"role": "system", "content": request.system.strip()})

    # **人格檔的穩定半**（2026-11-02）：同角色同人幾乎不變，放進固定段才吃得到前綴快取。
    # 易變半（心情／私記）在本輪段，仍然在歷史之後。
    stable_persona, _volatile_persona = _persona_split(request)
    if stable_persona:
        messages.append({"role": "system", "content": stable_persona})

    # **工作技能索引**（只有清單，正文用 skill.view 取）
    skills_block = str(request.meta.get("skills_block") or "").strip()
    if skills_block:
        messages.append({"role": "system", "content": "[[skills]]\n" + skills_block
                         + "\n[[/skills]]"})

    sticker_block = _sticker_block(request)
    if sticker_block:
        messages.append({"role": "system", "content": sticker_block})
    return messages


#: 〔私記〕的說明（她自己那層；**她永遠想得起來**，要不要說由她決定——owner 2026-11-03 線路 A）
_PRIVATE_NOTE = ("〔私記〕が付いているのは、あなたが自分だけのために残したもの。"
                 "話題として出すかどうか、この場の誰に話すかは、あなたが決めていい。"
                 "**「〔私記〕」という記号そのものは返答に書かない**（内部の目印）。\n")


def _ground_tags_on() -> bool:
    return str(os.environ.get("BLUEAGENT_GROUND_TAGS") or "1").strip().lower() \
        not in {"0", "off", "false", "no"}


def _memory_block(request: BrainRequest) -> str:
    """`[[memory]]`：召回的事實，按「**這一場的人也知道嗎**」分兩段（common-ground 第 2 步）。

    以前的標題是「あなたたちが前に本当に知っていること」——替對方擔保了他也知道；
    但裡面混著她的私記、聽別人說的、她自己的世界知識、生活流。對方沒聽過的事被她當成
    共同前提講（「あれ、どうだった？」），就是記憶假定。判定在 `RecallStage`
    （`shared_with_room`），這裡只排版。無主回合沒有對方 ⇒ 只有一段。
    `BLUEAGENT_GROUND_TAGS=0` ⇒ 回舊標題、不分段。
    """
    rows = [str(item).strip() for item in request.meta.get("recall", ())]
    flags = list(request.meta.get("recall_shared") or ())
    pairs = [(text, bool(flags[index]) if index < len(flags) else False)
             for index, text in enumerate(rows) if text]
    if not pairs:
        return ""
    texts = [text for text, _ in pairs]
    private_line = ("\n" + _PRIVATE_NOTE) if any("〔私記〕" in text for text in texts) else ""
    rule = "これだけを根拠に答え、分からなければ分からないと言う。作らない。\n"
    if not _ground_tags_on():
        body = "\n".join(f"{index + 1}. {text}" for index, text in enumerate(texts))
        return ("[[memory]]\n以下は、あなたたちが前に本当に知っていること。" + rule
                + private_line + body + "\n[[/memory]]")
    if request.meta.get("ownerless"):
        body = "\n".join(f"{index + 1}. {text}" for index, text in enumerate(texts))
        return "[[memory]]\n以下は、あなたが前から知っていること。" + rule + private_line + body \
            + "\n[[/memory]]"
    who = "この場の人たち" if request.meta.get("group") else "相手"
    shared = [text for text, known in pairs if known]
    mine = [text for text, known in pairs if not known]
    parts = ["[[memory]]\n以下は、あなたが前から知っていること。" + rule + private_line]
    number = 0
    if shared:
        parts.append(f"### {who}も知っていること（{who}がいた場で話した・聞いたこと）\n")
        for text in shared:
            number += 1
            parts.append(f"{number}. {text}\n")
    if mine:
        parts.append(f"### あなただけが知っていること（{who}は聞いていない。話すなら、"
                     "何の話かを一言伝えてから。「あれ」「例の」「前に言った」で指さない）\n")
        for text in mine:
            number += 1
            parts.append(f"{number}. {text}\n")
    return "".join(parts) + "[[/memory]]"


def _turn_blocks(request: BrainRequest) -> list[dict]:
    """③ 本輪段（GLOSSARY.md「本輪段」）：每回合都會變的幾塊，**放在歷史之後**（ADR-0020）。

    內容與以前一樣，只是位置換了：以前它們在 system 頭，把整段歷史的快取打掉。
    """
    names = _names(request)
    moment = float(request.meta.get("now") or time.time())
    messages: list[dict] = []
    # 人格檔的**易變半**（心情／她自己的私記）——每輪都在變，只能待在這裡
    _stable_persona, volatile_persona = _persona_split(request)
    if volatile_persona:
        messages.append({"role": "system", "content": volatile_persona})

    # 場上其他人說的話（群聊背景）。**與「我們兩個的歷史」分開**：
    # 這一段只是背景，不能當成她跟當前對象之間發生過的事（owner 2026-11-03）。
    ambient = [row for row in (request.meta.get("ambient") or ())
               if str(row.get("text") or "").strip()]
    if ambient:
        tail = ambient[-3:]
        body = "\n".join(_ambient_line(row, names=names, moment=moment) for row in tail)
        messages.append({"role": "system", "content": (
            "[[ambient]]\nこの場にいた**他の人**たちのやりとり（背景。あなたと相手の間の出来事ではない）。\n"
            "必要なら文脈として使うが、自分の経験として語らない。\n" + body + "\n[[/ambient]]")})

    # App 房裡她上次看過之後的發言（adapter 從 App 取；更前面的讓她自己用 app_chat_read 讀）。
    # 放在本輪段（歷史之後），不動固定段的前綴快取。
    room = str(request.meta.get("room_note") or "").strip()
    if room:
        messages.append({"role": "system", "content": "[[room]]\n" + room + "\n[[/room]]"})

    memory = _memory_block(request)
    if memory:
        messages.append({"role": "system", "content": memory})

    # **內心筆記**（子代理產出）：只給你參考，**不要照念、不要說出來**
    notes = [str(item).strip() for item in request.meta.get("subagent_notes") or ()
             if str(item).strip()]
    if notes:
        body = "\n".join(f"- {note}" for note in notes)
        messages.append({"role": "system", "content": (
            "[[notes]]\nこれはあなたがさっき自分で考えたこと（内心。相手が言ったことではない）。"
            "口調と内容の調整に使う。**そのまま読み上げない・このメモ自体に触れない**。"
            "相手のこの一言と関係のないメモは、この返事では持ち出さない（相手の話が先）。\n"
            + body + "\n[[/notes]]")})

    # 問句／依賴・指示／陳述先用規則判定，避免把指令誤讀成陳述。
    from agent.brain.domain.social_condition import utterance_kind
    kind = utterance_kind(request.text)
    label = {"request": "依頼・指示（やってほしい）",
             "question": "質問（答えを求めている）",
             "statement": "陳述（聞いてほしい）"}.get(kind, "")
    messages.append({"role": "system", "content": (
        "[[utterance]]\n"
        + (f"相手のこの一言は **{label}** の形（規則で判定）。\n" if label else
           "まず相手の一言が①質問 ②依頼・指示 ③陳述 のどれかを見分ける。\n")
        + "· 依頼・指示 ⇒ まず従う（できないなら理由を一言）。質問に言い換えない、"
          "相手が言っていないことを「〜したの？」と確認しない。\n"
          "· 質問 ⇒ 答える。\n"
          "· 陳述 ⇒ 受け止める（勝手に用事を決めない）。\n"
          "[[/utterance]]")})

    # 道具回合用完（2026-11-08）：這一輪是「不准再叫工具，只准報告」的收尾請求。
    # 以前 3 個工具回合用完還沒吐出字 ⇒ 整輪靜默（實測 11-08：主人只覺得她沒反應）。
    if request.meta.get("tool_limit"):
        messages.append({"role": "system", "content": (
            "[[tool_limit]]\n道具の回数を使い切った。**もう道具は呼べない**。"
            "今やったことと、できなかったことを、相手に短く報告する（2文まで）。"
            "できなかったことを「できた」と言わない。分からないことは分からないと言う。\n"
            "[[/tool_limit]]")})

    # 未渡しの依頼（2026-11-08）：主人要過、她還沒交的東西。**先交，再談別的**。
    # 沒有這一段時，aster 把自己的欠帳說成對方的欠帳（連五次「オーナーの番」）。
    delivery = str(request.meta.get("delivery_block") or "").strip()
    if delivery:
        messages.append({"role": "system", "content": (
            "[[todo]]\n主人が前に頼んで、まだ渡していないもの：\n" + delivery + "\n"
            "この返事の中で、まずこれを渡す（渡せないなら、できない理由を一言だけ言う）。"
            "渡していないのに「次はそちら」と相手に振らない。\n[[/todo]]")})

    # 承接：這條線上一段的摘要 `[[before]]`＋別條線剛剛的幾輪 `[[elsewhere]]`（D2，帶渠道來源）
    messages.extend(continuity_blocks(request.meta, names=names, moment=moment))

    clock_block = str(request.meta.get("clock_block") or "").strip()
    if clock_block:
        messages.append({"role": "system", "content": (
            "[[clock]]\n" + clock_block
            + "\nこの会話が今何度目か、前の発言からどれくらい経ったかは、上の「時間感」に書いてある。"
              "過去のことに触れるときはそれに従って**文章で**言う（「20 回前」「さっき」）。"
              "時間を自分で作らない。"
              "\n⚠️ **返答の本文には `#数字` のような記号を書かない**。"
              "番号は画面側の表示用で、返事には出てこない（読上げにも出ない）。\n[[/clock]]")})

    # 3D の立ち絵が見えている回（owner 2026-10-31「動作はモデルに選ばせる」）：返事の頭に動作を一つ付けられる
    motion_tags = [str(tag) for tag in (request.meta.get("motion_tags") or ()) if str(tag)]
    if motion_tags:
        messages.append({"role": "system", "content": (
            "[[motion]]\nあなたの姿は相手の画面に立っている。返事にふさわしい身ぶりがあれば、"
            "返事の**いちばん最初**に `[[motion:名前]]` を一つだけ書く（画面で動くだけで、文字にも声にも出ない）。\n"
            "使える名前：" + "、".join(motion_tags) + "\n"
            "合うものがなければ書かない。二つ以上は書かない。\n[[/motion]]")})

    # 形（owner 2026-11-11）：App と声（TTS）は短く、Discord の文字だけは縛らない。通話は下の [[call]] が同じ役。
    form = form_block(request.meta)              # グループは一言の形（messages_form.py）
    if form:
        messages.append({"role": "system", "content": form})
    # App の単人通話（owner 2026-10-29）：返事はそのまま声になる
    if request.meta.get("in_call"):
        messages.append({"role": "system", "content": (
            "[[call]]\nいま相手と電話で話している。あなたの返事はそのまま声で流れる。"
            "短く、話し言葉で（一、二文）。記号・箇条書き・URL・絵文字・括弧書きは使わない。"
            "相手の言葉は音声認識なので、少し崩れていても意味をくんで返す。\n[[/call]]")})
    return messages


#: 滾動窗口收縮的檔位（條）：預算逼得比 `max_history` 少時，向下取整到這個格。
#: 檔位＝一次要丟掉多少條；丟的那一回合歷史全部重讀一次，所以寧可少丟、丟多一點
#: （20 條≈10 個往復：60 條的窗口頂多收 3 次，而不是每回合收一次）。
WINDOW_STEP = 20


def _stable_window(kept: list[dict]) -> list[dict]:
    """把滾動窗口（GLOSSARY.md「滾動段」）收到「不會每回合都動開頭」的大小（ADR-0020）。

    `kept` 已經是預算內塞得下的最新那幾條（長度棘輪在 `build_messages` 上游已處理）。
    預算先撐不住時，條數**向下取整到 `WINDOW_STEP` 的檔位**：這樣每 10 條才有一次窗口
    跳動，而不是每回合掉一條（每掉一條，前綴快取就白費一次）。比一檔還小的窗口整支留著。

    收完之後，最舊那一條如果是她的發言就丟掉：那個提問已經不在窗口裡了。
    """
    room = len(kept)
    if room <= WINDOW_STEP:
        wanted = room
    else:
        wanted = (room // WINDOW_STEP) * WINDOW_STEP
    kept = kept[room - wanted:] if wanted else []
    while kept and kept[0]["role"] == "assistant":
        kept.pop(0)
    return kept


def _compose_current(request: BrainRequest, *, group: bool, principal_name: str) -> str:
    """⑤ 這一輪用戶訊息：群聊前綴 → followup 去重 → 人格檔貼前面 → 語言後綴。

    「先短回一句 → 再補完整答案」的第二段**不要重播開頭**（owner 2026-10-22：兩段都用
    同一個招呼語「おかわり…」，聽起來像重複）。人格檔貼在**這一輪用戶訊息的前面**
    （現役橋 `persona_prefix` 的位置，逐字對齊）：它每輪都變（心情／私記），所以必須在
    歷史之後，否則整段歷史的快取會被它打掉（ADR-0020）。
    """
    current = request.text.strip()
    if group and principal_name:
        current = f"{principal_name}：{current}"
    ack_text = str(request.meta.get("ack_text") or "").strip()
    if request.meta.get("followup") and ack_text:
        current = (
            f"{current}\n\n（さっき一度だけ返した：「{ack_text[:120]}」。"
            "この回は**新しい内容だけ**を足す：同じことを繰り返さない・挨拶し直さない・"
            "相手が言ったことを言い直さない。まだ伝えていない部分をそのまま話す。）")
    # 人格檔已拆兩半（穩定半在固定段、易變半在本輪段），這裡不再貼整塊
    # ——2026-11-02：整塊貼在這裡會讓 4,700 token 的人格永遠落在「已分叉」之後，
    #   前綴快取只剩 ~130 token 命中。語言鎖仍在下面那行尾端。
    speak_lang = str(request.meta.get("speak_lang") or "").strip().lower()
    suffix = TURN_SUFFIX.get(speak_lang, "")
    if suffix and not current.endswith(suffix):
        current = current + suffix
    return current


def build_messages(request: BrainRequest, *, max_history: int = 60,
                   max_chars: int = 36000, stats: dict | None = None) -> list[dict]:
    """一輪的訊息序列（**順序是 prompt 契約**，見 `docs/context-window.md`）。

    順序（owner 2026-11-01，照 ADR-0020 改）：

        ① 固定段（system）：人設＋stance＋群聊規則、技能索引、貼圖規則
        ② 歷史（滾動段）
        ③ 本輪段（system）：記憶／筆記／問句分類／承接／時間感／動作／通話
        ④ 遊戲段（user）→ ⑤ 這句話（user，前面貼人格檔）

    為什麼本輪段要**放在歷史之後**：服務商只快取請求**開頭**的公共前綴，而每回合都變的
    東西（記憶、時刻）以前擺在 system 頭 ⇒ 後面的歷史一個字都命不中。挪到歷史之後，
    「上一回合的請求去掉③④⑤」正好是這一回合的開頭。實測（我方 key、deepseek-flash、
    5 回合）：本輪段在頭時第 5 回合命中 51.7%，挪到歷史之後 76.1%，未命中 token 少 28%，
    而且差距隨對話變長繼續拉大（來源 ADR-0020 的 10 回合實驗是 32%）。
    """
    group = bool(request.meta.get("group"))
    #: 「多久之前」的基準：一律用這一輪的時間（測試可以塞 `now` 進來，結果就可重現）
    moment = float(request.meta.get("now") or time.time())
    names = _names(request)
    principal_name = str(request.meta.get("principal_name") or request.principal)
    # ① 固定段：同一位角色同一天對誰都一樣 ⇒ 可以整段命中快取
    messages = _fixed_blocks(request)
    fixed_chars = sum(len(str(item.get("content") or "")) for item in messages)

    history = list(request.meta.get("transcript") or ())
    if history:
        last = history[-1]
        if (str(last.get("role") or "") == "user"
                and str(last.get("text") or "") == request.text):
            history = history[:-1]
    limit = max(0, int(max_history))
    # ── 歷史長度的棘輪（ADR-0020）：**攒到 1.5 倍才一次砍回 N** ──────────────────
    # 以前每回合都 `history[-N:]`，等於每回合砍掉最舊一條 ⇒ 開頭每次都變、快取每次白費。
    # 現在：漲到 N 的 1.5 倍之前一條不動，超過了才一次砍回 N（中間那段前綴不動）。
    if limit:
        hard = int(limit * 3 / 2)
        if len(history) > hard:
            history = history[-limit:]
    else:
        history = []
    # 歷史先整理成「候選」，等本輪訊息組好、算出剩餘預算再從**最新往回填**
    #（2026-10-22：原本是固定 8 條 + 之後再砍，長句與工具結果會浪費或撐爆）。
    candidates: list[dict] = []
    for row in history:
        line = _history_line(row, group=group, names=names, moment=moment)
        if line is not None:
            candidates.append(line)

    current = _compose_current(request, group=group, principal_name=principal_name)

    # ── 預算分配（owner 2026-10-22：放寬到 36K）──────────────────────────────
    # 固定段與本輪訊息優先；剩下的全部給滾動段，從最新一條往回填。
    turn_blocks = _turn_blocks(request)
    budget = max(2000, int(max_chars))
    #: 單條歷史的硬上限：一句超長（貼了一大段 log）不該把整個窗口吃掉。
    per_message = max(400, int(budget * 0.15))

    def _clip(text: str) -> str:
        body = str(text or "")
        if len(body) <= per_message:
            return body
        keep = max(0, per_message - 6)
        return body[:keep] + "…（省略）"

    fixed = (sum(len(str(item.get("content") or "")) for item in messages)
             + sum(len(str(item.get("content") or "")) for item in turn_blocks))
    available = budget - fixed - len(current)
    kept: list[dict] = []
    for row in reversed(candidates):
        content = _clip(str(row.get("content") or ""))
        if len(content) > available:
            if not kept:
                # 連最近一條都放不下 ⇒ 截到剛好塞得進（至少讓它出現在上下文裡）
                content = content[: max(0, available)]
                if content:
                    kept.append({"role": row["role"], "content": content})
            break
        kept.append({"role": row["role"], "content": content})
        available -= len(content)
    kept.reverse()
    # ── 窗口按檔位收縮（ADR-0020）：**不要每回合掉一條** ─────────────────────
    # 前綴快取認的是「開頭一模一樣」；每回合砍掉最舊一條 ⇒ 每回合都失效一次。
    # 所以：條數在 `max_history` 以內就全留（新的接在尾巴，不影響前綴）；
    # 預算逼得比 max_history 少時，向下取整到 `WINDOW_STEP` 的檔位，最多每 10 條才動一次。
    kept = _stable_window(kept)
    messages.extend(kept)
    # ③ 本輪段：每回合都變的東西 —— **併進最後那一條用戶訊息**（2026-11-02 改）
    #
    # 為什麼不再當獨立訊息：Strata 的對話快取只在「這一次的 messages 是上一次的嚴格延伸」
    # 時才命中（實測：把最後一條換掉 = 0 命中；在尾巴追加 = 命中 6,740/6,747）。
    # 本輪段若單獨成一條訊息卡在歷史之後，下一輪它就被新的歷史擠到別的位置 ⇒ 不是延伸 ⇒ 全部重讀。
    # 併進用戶訊息後，它就成了歷史的一部分，每輪只是往後接，前綴整段命中。
    # 語言鎖仍在最後（`_compose_current` 的尾端）。
    turn_text = "\n\n".join(str(item.get("content") or "") for item in turn_blocks
                            if str(item.get("content") or "").strip())
    # ── 遊戲段（owner 2026-10-22）：單獨一段，**不從上面的 budget 扣**（它有自身上限）。
    game_block = str(request.meta.get("game_block") or "").strip()
    if game_block:
        cap = int(request.meta.get("game_block_chars") or 3000)
        turn_text = f"{game_block[:cap]}\n\n{turn_text}" if turn_text else game_block[:cap]
    if turn_text:
        current = f"{turn_text}\n\n{current}"
    messages.append({"role": "user", "content": current})

    if stats is not None:
        # 分段字數：給 `brain/turn_trace.py` 印「這一輪的 prompt 花在哪」
        stats.update({"fixed": fixed_chars,
                      "history": sum(len(str(item.get("content") or "")) for item in kept),
                      "turn": len(turn_text), "current": len(current),
                      "messages": len(messages), "history_items": len(kept)})
    # 工具调用/结果放在当前用户消息之后；它们是这一轮的脚手架，不参与 history 裁剪。
    def wire_name(name) -> str:
        # trace 保留內部原名；回灌給端點的歷史必須和首輪 tools 使用同一個合法名。
        return "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in str(name or ""))

    for round_trace in request.meta.get("tool_trace") or ():
        calls = round_trace.get("calls") if isinstance(round_trace, dict) else None
        results = round_trace.get("results") if isinstance(round_trace, dict) else None
        if not isinstance(calls, list) or not calls:
            continue
        tool_calls = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            arguments = call.get("arguments")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments or {}, ensure_ascii=False)
            tool_calls.append({
                "id": str(call.get("id") or ""),
                "type": "function",
                "function": {"name": wire_name(call.get("name")),
                             "arguments": arguments},
            })
        if tool_calls:
            messages.append({"role": "assistant", "content": None,
                             "tool_calls": tool_calls})
        for result in results or ():
            if isinstance(result, dict):
                messages.append({"role": "tool",
                                 "tool_call_id": str(result.get("call_id") or ""),
                                 "content": str(result.get("content") or "")})
    _dump_debug(messages)
    return messages


__all__ = ["build_messages"]
