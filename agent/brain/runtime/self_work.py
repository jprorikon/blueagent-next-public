"""她自己的事也要**真的去做**（owner 2026-10-24）。

為什麼要有這支：活動的步驟以前只是「睡 0.65 秒 + 寫一行標題」，連工具都碰不到——
所以她說「翻規則書，找三條沒用過的判定」時，實際上什麼都沒查。就算讓模型寫一段內容，
沒有工具的模型只會**編**：講得像查過，其實是幻覺。這比空白更糟。

這裡給自主任務一條真的工作迴圈（跟主對話共用同一份 ToolRegistry 與 BrainPort）：
  ① 把可用的工具規格放進請求（`mode="work"`，只放允許的工具）
  ② 模型要工具 ⇒ 真的執行、結果回灌 → 再來一輪（`max_rounds` 上限）
  ③ 回傳 `{text, trace}`：`trace` 是這次真的用了哪些工具（寫進筆記＝可驗收）

失敗語義：模型沒吐內容／工具全失敗 ⇒ 回 `ok=False`，呼叫端保留原步驟（不假裝做過）。

**工具面是白名單**（2026-10-24 review）：產出會進 public 日誌與世界可見的筆記事實，
而提示詞裡還有網頁摘要（不可信內容）。所以這裡只給「查公開資料」的工具——
不給 `memory.search`（會把主人的私事洗進公開筆記）、不給 `voice.*`（她自己做事時不該撥電話）、
不給委任。白名單以外的呼叫**不執行**，只記進 trace。
"""

from __future__ import annotations

import json
import os

from typing import Any, Callable, Mapping

#: 発帖（owner 2026-11-08：**想发了就发**——発帖は SNS の社会的行動であって、
#: 活動の成果報告／任務の延長ではない）。だから「成果があるとき」ではなく
#: 「書きたい・見てほしい・反応がほしいと思ったとき」に自分で出す。
POST_NOTE = (
    "・公共区（みんなの投稿）は SNS と同じ場所。**言いたくなったら自分で投稿してよい**"
    "（`mcp_board_post`、1日3本まで、投稿は有料）。\n"
    "・投稿の動機は「成果の報告」ではない：見てほしい／反応がほしい／今の気分を置いておきたい／"
    "誰かに伝えたい——そういう理由で書く。活動の途中でも、活動と関係なくてもよい。\n"
    "・読むのは他の人（主人やほかの役）。初めて読む人にも分かるように、何の話かを最初の一文で書く"
    "（指示語・表は使わない）。\n"
    "・反応（いいね／返信）が来たら、それはあなたへの反応として届く。\n"
)

WORK_PROMPT = (
    "この一歩は**自分の手で最後までやり切る**。説明でもなければ計画でもない。\n"
    "ルール：\n"
    "・外の情報が要るなら、まず道具で調べる（memory.search／mcp_board_feed／mcp_board_get／skill.* など）。調べたことはそのまま書く。\n"
    # 自分のマシン（owner 2026-11-03）：道具があるだけだと使われないので、プロンプトでも一度名指しする
    "・**自分専用の Linux マシン（`terminal`）がある**：ファイルを作る／道具を入れる／計算する／"
    "ブラウザで実際のページを開く——手を動かしたほうが早いことは自分でやる。やったことはそのまま書ける。\n"
    "・調べても分からなければ「分からなかった」と正直に書く。**出どころや中身を作り話にしない**。\n"
    "・**実際に道具を呼んでいないなら、「調べた／めくった／見つけた」とは書かない**——書けるのは「記憶では／覚えている限り」だけ。\n"
    "・最後に一人称で成果を2〜4文。次に自分で話せるくらい具体的に（調べて分かったこと／気づいたこと）。\n"
    "・**殘す価値のあるやり方は技能にする**（`mcp_skills_write`）：完了条件と失敗時を必ず書き、"
    "実際に 1 回やった証拠を添えて `mcp_skills_trial` で試用を記録する（成功 2 回で stable）。\n"
    + POST_NOTE +
    "・誰にも話しかけない、やることを書き出さない。\n\n"
    "人物：{persona}\n活動：{activity}\nこの一歩：{step}\n言葉：{lang}\n"
)


#: 公開步驟的追加提示（owner 2026-11-08：「感觉有点没头没尾的。是不是没意识到发出来的
#: 东西是会被人看到的」）。公開的那一步會原樣貼到公共區 ⇒ 寫的時候就要知道有讀者：
#: ① 第一句先說「これは何の話か」（活動名）；② 不用指示語（さっきの／この件／二人）；
#: ③ 不畫表、不帶 markdown 記號；④ 仍是一人稱、不對著誰說話。
PUBLIC_NOTE = (
    "\n［この一歩は**公開の記録**に残る（主人が読める。投稿するかは自分で決める）］\n"
    "・書き出しの一文で「何の話か」が分かるようにする（活動名か、対象の名前を入れる）。\n"
    "・「さっきの」「この件」「二人」のような指示語で済ませない——読む人には分からない。名前で書く。\n"
    "・見出し記号や表（###、|---|）は使わない。ふつうの文で3〜4文。\n"
    "・誰かに話しかける文体にはしない（独り言でもない。読む人がいる、という距離で書く）。\n"
)


#: 做作品那一輪的 token 預算（全站預設 400 只夠 2～4 句，寫不出 300 字成品）
ARTEFACT_MAX_TOKENS = 1200

#: 「做小作品」用的提示詞（owner 2026-10-24）。
#: 實測踩到的坑：沿用記事型提示詞（「寫下你剛剛實際做完的內容」）⇒ 她會**敘述自己寫作的過程**
#: （「本來想寫三百字，結果只寫兩句就卡住了」），成品檔裡沒有作品，只有她卡住的感想。
ARTEFACT_PROMPT = (
    "いま**作品そのものを書き出す**。どう書いたかの説明でも、計画でも、反省でもない。\n"
    "ルール：\n"
    "・作品の中身をそのまま出す（場面／表／提案、仕様どおりに）。小見出しは使ってよい。\n"
    "・「本当はこう書くつもりだった」「途中で詰まった」「消して書き直した」——こういう書き方の過程の叙述や、一人称の回想は**書かない**。\n"
    "・外の資料が要るときだけ調べる（Firecrawl／notes.read／mcp_board_feed）。分からなければ出典は書かないし、作り話もしない。\n"
    "・誰にも話しかけない、やることを書き出さない、自分への評も書かない。\n\n"
    "人物：{persona}\n活動：{activity}\nこの一歩：{step}\n言葉：{lang}\n"
)


#: 搜尋與正文不再走付費 DeepSeek API；全站統一使用自架 Firecrawl。
FIRECRAWL_SEARCH = "mcp_firecrawl_firecrawl_search"
FIRECRAWL_SCRAPE = "mcp_firecrawl_firecrawl_scrape"
FIRECRAWL_MAP = "mcp_firecrawl_firecrawl_map"
FIRECRAWL_CRAWL = "mcp_firecrawl_firecrawl_crawl"

#: 自主任務能用的工具（registry 名）。產出是公開的 ⇒ 只放讀公開資料的。
SELF_WORK_TOOLS: tuple[str, ...] = (FIRECRAWL_SEARCH, FIRECRAWL_SCRAPE,
                                   FIRECRAWL_MAP, FIRECRAWL_CRAWL,
                                   "skill.view", "notes.read", "terminal",
                                   # 公共區（owner 2026-11-07）：從**帖**讀，不再讀活動記錄。
                                   # 用現役 MCP `board`（不另寫第二套）；reply/like 在 MCP 那側有每日上限。
                                   "mcp_board_feed", "mcp_board_get",
                                   "mcp_board_post", "mcp_board_reply", "mcp_board_like",
                                   # 技能學習（owner 2026-11-03）：寫／試用／退役，成功 2 次升 stable
                                   "mcp_skills_list", "mcp_skills_view", "mcp_skills_write",
                                   "mcp_skills_trial", "mcp_skills_retire",
                                   # 任務台帳走外置 MCP（owner 2026-11-03）：看板／日曆／插任務／交狀態
                                   "mcp_tasks_board", "mcp_tasks_calendar", "mcp_tasks_add",
                                   "mcp_tasks_state", "mcp_tasks_note", "mcp_tasks_ack")


# ── 外網不可信輸入後的工具降權（owner 2026-11-16 拍板 A.13／D4 ②）──────────────────
#: 讀到外面網頁內容的工具（`terminal` 只有 `web …` 指令算）
WEB_READ_TOOLS = frozenset({FIRECRAWL_SEARCH, FIRECRAWL_SCRAPE, FIRECRAWL_MAP, FIRECRAWL_CRAWL})
#: 高副作用：對外發言（公共區發帖／回帖）與會**留下來指揮她以後**的寫入（技能、任務）。
#: 同一步裡已經讀過外網 ⇒ 確定性擋下（不靠模型判斷）；下一步只帶她自己寫的成果，不帶網頁原文，
#: 那時要發、要寫照常可以——是「延到下一步」，不是取消（owner 2026-11-08「想发了就发」仍成立）。
GATED_AFTER_WEB = frozenset({"mcp_board_post", "mcp_board_reply", "mcp_skills_write",
                             "mcp_tasks_add"})
UNTRUSTED_DENIAL = "untrusted_input_in_step"


def is_web_read(name: str, arguments) -> bool:
    if name in WEB_READ_TOOLS:
        return True
    command = str((arguments or {}).get("cmd") or "").lstrip() if isinstance(arguments, dict) else ""
    return name == "terminal" and (command == "web" or command.startswith("web "))


def _notes_block(tools, context_factory, allowed, activity: str, step: str, *,
                 long_context: bool, trace: list) -> str:
    """複習／自測／整理：先把她的筆記本體抓進來（模型不會自己叫 notes.read）。"""
    if "notes.read" not in allowed.values() or not any(
            mark in f"{activity}{step}" for mark in NOTE_MARKS):
        return ""
    context = context_factory() if context_factory is not None else None
    if context is None:
        return ""
    days = 30 if long_context else 14
    limit = 40 if long_context else 24
    result = tools.run("notes.read", {"days": days, "limit": limit}, context)
    digest = _digest(result, max_len=(4000 if long_context else 2400))
    has_material = bool(result.ok) and int((result.data or {}).get("count") or 0) > 0
    if digest and has_material:
        trace.append({"name": "notes.read", "ok": bool(result.ok),
                      "arguments": {"days": str(days), "limit": str(limit)},
                      "preview": digest[:120]})
        return ("\n\n［自分のノート（notes.read で実際に読めたもの。復習はこれだけを根拠にする）］\n"
                f"{digest}\n")
    trace.append({"name": "notes.read", "ok": False, "error": "no_notes"})
    return ("\n\n［notes.read では自分のノートが読めなかった］——なら、復習できる材料は無いと"
            "正直に言う。ノートの中身を作らない。\n")


def _turn_events(target, request):
    """跑一輪，出錯回 None（雲端不穩時由上層回落本地，不讓整步失敗）。"""
    try:
        return list(target.run_turn(request))
    except Exception as exc:                       # noqa: BLE001 - 上層會回落
        print(f"  step: 呼叫失敗（{type(exc).__name__}: {str(exc)[:80]}）", flush=True)
        return None


def _allowed_tools(tools) -> tuple[list[dict], dict[str, str]]:
    """白名單裡實際註冊、且工作模式放行的工具 ⇒ `(schemas, 呼叫名→registry 名)`。

    模型回的是 schema 上的函式名，registry 名與函式名都收（MCP 兩者通常相同）。
    """
    schemas: list[dict] = []
    names: dict[str, str] = {}
    get = getattr(tools, "get", None)
    for name in SELF_WORK_TOOLS:
        tool = get(name) if get is not None else None
        schema = getattr(tool, "schema", None) if tool is not None else None
        if not schema or not tools.allowed(tool, mode="work", is_owner=True):
            continue
        schemas.append(schema)
        names[name] = name
        function = str((schema.get("function") or {}).get("name") or "")
        if function:
            names[function] = name
    return schemas, names


def _digest(result, *, max_len: int = 400) -> str:
    """把工具結果壓成給模型看的一段字。

    2026-10-24 實測的坑：以前只讀 `summary`／`text` 兩個 key，而 `activity.peek` 回的是
    `{"entity","lines","count"}` ⇒ 模型收到的是「［activity.peek 的結果］（空白）」⇒
    她照樣寫「查不到」（工具明明有回內容）。所以這裡按實回形狀取：
    `summary`／`text` → `lines` 串起來 → 其餘 JSON 截斷。
    """
    data = getattr(result, "data", None) or {}
    error = str(getattr(result, "error", "") or "")
    if not getattr(result, "ok", False):
        return error
    for key in ("summary", "text"):
        value = str(data.get(key) or "").strip()
        if value:
            return value[:max_len]
    lines = data.get("lines")
    if isinstance(lines, (list, tuple)) and lines:
        return " ／ ".join(str(item) for item in lines if str(item).strip())[:max_len]
    if data:
        try:
            import json as _json
            return _json.dumps(data, ensure_ascii=False)[:max_len]
        except (TypeError, ValueError):
            return str(data)[:max_len]
    return ""


#: 這些字眼代表「這一步要讀自己的舊筆記」⇒ 直接先把筆記本體抓進 prompt（複習／自測用）
NOTE_MARKS = (  # lang-ok: 比對步驟標題用的詞表
    "筆記", "笔记", "ノート", "記錄", "记录", "複習", "复习", "自測", "自测")

#: 這些字眼代表「這一步需要外部資料」（翻／查／搜…）
LOOKUP_MARKS = ("查", "搜", "翻", "調べ", "找", "讀", "読", "look up", "search")


def _prefetch(tools, context, activity: str, step: str, *, limit: int = 3) -> str:
    """真的查一次自架 Firecrawl；MCP 文字結果直接留作證據，查不到回空，不拋例外。"""
    try:
        result = tools.run(FIRECRAWL_SEARCH,
                           {"query": f"{step} {activity}".strip(), "limit": limit}, context)
    except Exception:                                   # noqa: BLE001
        return ""
    if not result.ok:
        return ""
    data = result.data or {}
    if "text" in data and not str(data.get("text") or "").strip():
        return ""
    digest = _digest(result, max_len=1200)
    return "" if digest == "（沒有結果）" else digest


class _CallBudget:
    """一步之內的工具呼叫預算（白名單另管，這裡只算「還能叫幾次／這個是不是重複」）。"""

    def __init__(self, max_calls: int, *, tainted: bool = False) -> None:
        self.max_calls = max(1, int(max_calls))
        self.executed = 0
        self.called: set[str] = set()
        #: 這一步的 prompt 裡已經有外網內容（預查或 web 工具）⇒ 高副作用工具不放行
        self.tainted = bool(tainted)

    def deny_reason(self, name: str, arguments) -> str:
        """回空字串＝放行；回理由＝這次不執行（`duplicate_in_step`／`step_call_budget`）。"""
        signature = name + "|" + json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False)
        if signature in self.called:
            return "duplicate_in_step"
        if self.executed >= self.max_calls:
            return "step_call_budget"
        self.called.add(signature)
        self.executed += 1
        return ""


def _run_calls(calls, *, allowed, tools, context_factory, trace: list, prompt: str,
               budget: "_CallBudget") -> str:
    """把模型這一輪要的工具真的跑一遍，結果接回 prompt（白名單／去重／步內預算都在這裡擋）。"""
    for call in calls:
        asked = str(call.get("name") or "")
        name = allowed.get(asked, "")
        if not name:
            trace.append({"name": asked, "ok": False, "error": "not_in_self_work_whitelist"})
            prompt += f"\n\n［{asked}］この道具は自分の仕事では使えない。"
            continue
        if budget.tainted and name in GATED_AFTER_WEB:
            trace.append({"name": name, "ok": False, "error": UNTRUSTED_DENIAL})
            prompt += (f"\n\n［{name}］外のページを読んだこの手順では、投稿・返信・技能や予定の書き込みはしない。"
                       "やりたければ次の手順で、自分の言葉で。")
            continue
        denied = budget.deny_reason(name, call.get("arguments"))
        if denied:
            trace.append({"name": name, "ok": False, "error": denied})
            prompt += (f"\n\n［{name}］この手順で道具を使えるのは {budget.max_calls} 回まで。"
                       "同じ呼び出しは繰り返さない。いま手元にある結果で書き切る。")
            continue
        context = context_factory() if context_factory is not None else None
        if context is None:
            trace.append({"name": name, "ok": False, "error": "no_context"})
            continue
        try:
            result = tools.run(name, dict(call.get("arguments") or {}), context)
        except Exception as exc:                        # noqa: BLE001 - 工具壞了不炸她
            trace.append({"name": name, "ok": False,
                          "error": f"{type(exc).__name__}: {exc}"})
            continue
        digest = _digest(result)
        # 留證據（可驗收）：模型到底傳了什麼、工具回了什麼。
        # 2026-10-24 就是因為只記名字，才查不出「peek 有空結果她還寫查不到」的原因。
        trace.append({"name": name, "ok": bool(result.ok),
                      "error": str(result.error or ""),
                      "arguments": {str(key): str(value)[:40]
                                    for key, value in (call.get("arguments") or {}).items()},
                      "preview": digest[:120]})
        prompt += f"\n\n［{name} の結果］{digest}"
        if result.ok and is_web_read(name, call.get("arguments")):
            budget.tainted = True
    return prompt


def run_step(brain, tools, *, entity: str, activity: str, step: str,
             persona: str = "", principal: str = "owner", max_rounds: int = 3,
             lang: str = "", brief: str = "", form: str = "note",
             history=(), progress: str = "", context_block: str = "",
             alt_brain=None, alt_reason: str = "", long_context: bool = False,
             context_factory: Callable[[], Any] | None = None,
             public: bool = False, log=None) -> dict:
    """跑一步「真的工作」。回 `{ok, text, trace, rounds}`。

    `context_factory()` 要回一個 `ToolContext`（每次呼叫重給，避免跨輪共用狀態）。
    `brain` 是 BrainPort（有 `run_turn(request)`），`tools` 是 ToolRegistry。

    `history`：這一件事前面幾步做過什麼（`["步驟 → 結果", …]`）。沒有它，
    「把那一條重寫成一句」這種步驟就不知道「那一條」是哪一條（owner 2026-10-27）。
    """
    from agent.brain.ports import BrainRequest

    if brain is None or tools is None:
        return {"ok": False, "text": "", "trace": [], "reason": "not_configured"}
    schemas, allowed = _allowed_tools(tools)
    # 她講日文 ⇒ 筆記也要日文（實測 2026-10-24：沒指定時筆記變成中文）
    language = {"ja": "日本語で書く", "zh": "中国語で書く"}.get(str(lang or "").strip().lower(),
                                                    "いつも話している言葉で書く")
    template = ARTEFACT_PROMPT if str(form) == "work" else WORK_PROMPT
    # 1400 字：人格前段（200）＋她的関心（最多 700）＋餘裕。以前是 400——関心一接進來就被切掉，
    # 等於白寫（owner 2026-11-03）。
    # 4000 字：她自己做事沒有回話延遲壓力（owner 2026-11-03），人格／関心可以整段給
    prompt = template.format(persona=str(persona or "")[:4000], activity=activity,
                             step=step, lang=language)
    # 公開的那一步會被原樣貼到公共區 ⇒ 寫之前就告訴她有讀者（owner 2026-11-08）
    if public:
        prompt += PUBLIC_NOTE
    # 做作品要寫得完：全站預設 400 tokens 對「300 字成品」不夠（實測只吐出兩句）。
    meta_extra = {"max_tokens": ARTEFACT_MAX_TOKENS} if str(form) == "work" else {}
    # 這一套活動自己的規格（做成品／長度／格式）——寫在規則後面，模型才不會用「2～4 句」蓋掉
    if str(brief or "").strip():
        prompt += f"\n\nこの活動の仕様（上の「2〜4文」より優先）：{str(brief).strip()}\n"
    # 進度（owner 2026-11-03）：她在**整件事的第幾步**。沒有它，重啟後或隔了幾小時再續，
    # 她只知道「這一步要做什麼」，不知道「之前走到哪」。
    if str(progress or "").strip():
        prompt += f"\n\n［この活動の進み具合］{str(progress).strip()}\n"
    # 「最近の自分」等外部拼好的段（生活日誌）：她自己做事時的連貫感靠它
    if str(context_block or "").strip():
        prompt += f"\n\n{str(context_block).strip()}\n"
    past = [str(item) for item in (history or ()) if str(item).strip()]
    if past:
        prompt += ("\n\n［この件で自分がもう済ませた部分（流れを保つ。やり終えたことを繰り返さない、"
                   "指示を言い直さない）］\n" + "\n".join(f"・{item}" for item in past[-(40 if long_context else 20):]) + "\n")
    trace: list[dict] = []
    # 實測（2026-10-24）：提示詞寫「先查再寫」，模型還是不叫工具，直接照樣編一段
    # 「我翻了幾頁…」。所以這裡**先自己查**（確定性），把結果放進提示詞當證據。
    # 複習／自測：**先把她的筆記本體抓進來**。實測（2026-10-25）：只寫「讀自己的筆記」，
    # 模型不叫 `notes.read`，直接憑空寫出根本不存在的筆記（她答的「過擬合／dropout」不是她的筆記）。
    prompt += _notes_block(tools, context_factory, allowed, activity, step,
                           long_context=long_context, trace=trace)
    _needs_lookup = any(mark in f"{activity}{step}" for mark in LOOKUP_MARKS)
    if _needs_lookup and FIRECRAWL_SEARCH in allowed.values():
        context = context_factory() if context_factory is not None else None
        if context is None:
            from agent.brain.runtime.tools.base import ToolContext
            context = ToolContext(entity=str(entity), principal=str(principal),
                                  thread_id=f"self:{entity}", source="self", voice=None,
                                  mode="work")
        digest = _prefetch(tools, context, activity, step)
        prefetched = bool(digest)
        if digest:
            prompt += f"\n\n［Firecrawl で実際に拾えた資料（本物の結果）］\n{digest}\n"
            trace.append({"name": FIRECRAWL_SEARCH, "ok": True})
        else:
            prompt += ("\n\n［Firecrawl は引いたが何も出なかった］——なら、分からなかったと正直に言う。"
                       "見つけたふりをしない。「何ページかめくった」とも書かない。\n")
            trace.append({"name": FIRECRAWL_SEARCH, "ok": False, "error": "no_results"})
    else:
        prefetched = False
    text = ""
    rounds = 0
    # ★ 2026-10-31（P2）：一步之內的工具呼叫要收斂。實測她一步能叫 7 次搜尋、
    #   3 次 activity.peek、4 次 notes.read，而同樣那一步一天還重跑 40 次 ⇒ 幾乎全是重複勞動。
    #   現在：一步最多 `BLUEAGENT_SELF_WORK_MAX_CALLS`（預設 2）次；同一個 (工具+參數) 一步只做一次。
    budget = _CallBudget(int(os.environ.get("BLUEAGENT_SELF_WORK_MAX_CALLS") or 2),
                         tainted=prefetched)
    # 技能整理／失敗復盤這種長上下文步驟可以走另一顆腦（owner 2026-11-03：DeepSeek）；
    # 雲端出錯就**回落本地**，不讓她這一步空掉。
    step_brain = brain
    if alt_brain is not None and alt_reason:
        step_brain = alt_brain
    for round_index in range(max(1, int(max_rounds))):
        rounds = round_index + 1
        request = BrainRequest(
            thread_id=f"self:{entity}", entity=str(entity), principal=str(principal),
            text=prompt, mode="work",
            meta={"tools": schemas, "tool_trace": list(trace),
                  "speak_lang": "", "self_task": True, **meta_extra},
        )
        calls: list[dict] = []
        text = ""
        if step_brain is not brain:
            print(f"  step[{entity}]: 走 {alt_reason}", flush=True)
        events = _turn_events(step_brain, request)
        if events is None:                         # 雲端那顆炸了 ⇒ 本地重跑同一份
            print(f"  step[{entity}]: {alt_reason} 失敗 ⇒ 回落本地腦", flush=True)
            step_brain = brain
            events = _turn_events(step_brain, request) or []
        for kind, payload in events:
            if str(kind) == "tool_call":
                calls.append(dict(payload))
            elif str(kind) == "done":
                text = str((payload or {}).get("text") or "").strip()
        if not calls:
            break
        # 真的執行工具，結果接進下一輪
        prompt = _run_calls(calls, allowed=allowed, tools=tools, context_factory=context_factory,
                            trace=trace, prompt=prompt, budget=budget)
        if text:
            break
    # 實測（2026-10-24）：模型很容易「只叫工具、不寫成果」（最後一輪回來是空的），
    # 那樣就會退回純敘述、被標成未查證。這裡補一個**收尾回合**：工具查完了，
    # 不再給工具，只要求把成果寫出來。
    if not text and trace:
        final = BrainRequest(
            thread_id=f"self:{entity}", entity=str(entity), principal=str(principal),
            text=prompt + "\n\n道具での確認は終わった。ここからは**成果だけ**を書く：2〜4文、"
                          "調べて分かったことを具体的に。分からなかったことは分からなかったと書く。\n",
            mode="work", meta={"tools": [], "tool_trace": list(trace),
                               "speak_lang": "", "self_task": True, **meta_extra},
        )
        rounds += 1
        for kind, payload in step_brain.run_turn(final):
            if str(kind) == "done":
                text = str((payload or {}).get("text") or "").strip()
    ok = bool(text) and any(item.get("ok") for item in trace) if trace else bool(text)
    if log is not None and trace:
        try:
            detail = "；".join(
                f"{item['name']}({item.get('arguments') or {}})"
                f"→{'ok' if item.get('ok') else (item.get('error') or 'fail')}"
                f"{'：' + item['preview'][:40] if item.get('preview') else ''}"
                for item in trace[:4])
            log(f"self-work[{entity}] 工具 {len(trace)} 次：{detail[:300]}")
        except Exception:                               # noqa: BLE001
            pass
    return {"ok": ok, "text": text, "trace": trace, "rounds": rounds}
