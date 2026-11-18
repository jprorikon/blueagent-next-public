"""她的「自己做事」：每一步怎麼寫（從 `life.py` 拆出來，2026-10-27）。

`life.py` 留活動庫／輪換／排程；這裡只負責**執行一步**：叫工具或叫模型 → 組出正文
→ 落筆記 → 記日誌／記憶 → 回報進度與「還開不開著」。

拆的理由：life.py 已經 670 行（檔案棘輪 600）、`make_step_fn` 也超過函式長度上限。
"""

from __future__ import annotations

import json
import os
import pathlib
import time

from agent.brain.runtime.fileperm import adopt_owner
from agent.brain.runtime.inner.work_output import merge_section
from agent.brain.ports import FactSinkPort
from agent.brain.runtime.activity_log import ActivityLog
from agent.brain.runtime.activity_log import PRIVATE, PUBLIC  # noqa: F401
from agent.brain.domain.entity import EntityRegistry

#: 真的打到外面去的工具（會花頻寬）：自主任務每用一次就多扣一筆體力。
#: 搜尋／抓頁走自架 Firecrawl；JS 頁走 terminal 在 VM 執行 `web ...`，不能把所有終端命令都算外網。
NET_TOOLS = frozenset({"mcp_firecrawl_firecrawl_search", "mcp_firecrawl_firecrawl_scrape",
                       "mcp_firecrawl_firecrawl_map", "mcp_firecrawl_firecrawl_crawl",
                       # 發帖（owner 2026-11-08「发帖可以给她。但是和其他活动一样，有消耗」）：
                       # 一次發帖按一次外網計價，明細併進這件活動的帳。
                       "mcp_board_post"})
NET_COST_ENV = "BLUEAGENT_BUDGET_NET_COST"
#: 這些不是「用過的工具」，是被規則擋下來的（蓋章時要排除，也不收網路費）
_DENIED_CALLS = frozenset({"step_call_budget", "duplicate_in_step", "untrusted_input_in_step"})
#: trace 裡這些錯誤代表**根本沒送出去**（白名單擋掉／沒有 context／步內預算或重複被擋）⇒ 不收網路費
NOT_SENT = frozenset({"not_in_self_work_whitelist", "no_context"}) | _DENIED_CALLS

from agent.brain.runtime.inner.rooms import self_room  # noqa: E402
from agent.brain.memory.facts import LIFE_TTL_DAYS  # noqa: E402




def _accepts_keyword(fn, name: str) -> bool:
    """這個 callable 收不收 `name` 這個關鍵字參數（不收就別傳，否則 TypeError）。

    有 `**kwargs` 的也算收——包裝函式常寫成 `(title, step, **kwargs)`，
    以前只認具名參數，`history`／`brief` 就這樣被默默吞掉（2026-10-27）。
    """
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(item.kind is inspect.Parameter.VAR_KEYWORD for item in params.values())

#: 每一步「真的做事」的提示詞（owner 2026-10-24：以前只是睡 0.65 秒寫一行模板）
STEP_PROMPT = (
    "あなたはある人物を演じていて、自分で決めたことをしている最中です。**いま実際にやり終えた中身**を一人称で書いてください。\n"
    "ルール：2〜4文、具体的に（調べたこと／見たもの／腑に落ちた点を書く）。"
    "「終わりました」だけで済ませない、やることを並べない、誰にも話しかけない。\n\n"
    "人物：{persona}\n活動：{activity}\nこの一歩：{step}\n"
)

#: 一天結束時的自我總結（owner 2026-10-24：她要有「我最近在過什麼日子」的連續性）
REFLECT_PROMPT = (
    "あなたはある人物を演じています。以下は{days}あなたがしたことと話したこと（抜粋）です。\n"
    "**三行の日記**を一人称で書いてください：① 今日いちばん気になったこと ② 学んだ／気づいたこと ③ 明日やりたいこと。\n"
    "口語で書く、箇条書きの記号は使わない、誰にも話しかけない、起きていないことを作り話にしない。\n\n{material}\n"
)


def _charge_network(activity: Activity, tools_used: list, step_text: str, *,
                    budget=None, net_cost: float | None = None) -> None:
    """這一步打到外面去了 ⇒ **累積成明細**，不再當場扣錢（owner 2026-11-01 照 aster 的設計改）。

    aster 的意見（2026-11-01）：「一步 1 RP ＋ 同一步三次外網 4.5 ⇒ 那一步實際是 5.5，但帳面上
    看不出『這步很貴』，只看到四筆扣款」⇒ **記成一筆帶明細**，不要記四筆。

    所以這裡只把「這次打了幾次外網、值多少」累到 `activity.meta`，由 `LifeLoop` 在**這件活動結束時**
    併成一筆收（`net_points`／`net_hits`）。價格本身仍是 `BLUEAGENT_BUDGET_NET_COST`（不變）。
    """
    price = (float(net_cost) if net_cost is not None
             else float(os.environ.get(NET_COST_ENV) or 0.0))
    if budget is None or price <= 0:
        return
    def outbound(item) -> bool:
        row = item or {}
        name = str(row.get("name") or "").strip()
        if str(row.get("error") or "") in NOT_SENT:
            return False
        if name in NET_TOOLS:
            return True
        command = str((row.get("arguments") or {}).get("cmd") or "").lstrip()
        return name == "terminal" and (command == "web" or command.startswith("web "))

    hits = sum(1 for item in tools_used if outbound(item))
    if not hits:
        return
    meta = getattr(activity, "meta", None)
    if not isinstance(meta, dict):
        return
    meta["net_points"] = round(float(meta.get("net_points") or 0.0) + price * hits, 3)
    meta["net_hits"] = int(meta.get("net_hits") or 0) + hits
    meta.setdefault("net_steps", [])
    if len(meta["net_steps"]) < 20:
        meta["net_steps"].append(f"{step_text[:24]} ×{hits}")



#: 這一步要寫什麼（模組層函式：make_step_fn 已經太長，拆出來才過得了長度棘輪）
#: 「這件事還沒弄完」的語氣標記：收尾時最後一句命中就當作「還開著」，
#: 下次輪換優先接著做（owner 2026-10-27 第 4 條）。
OPEN_MARKS = (  # lang-ok: 比對她輸出用的詞表，不進 prompt
              "沒弄懂", "没弄懂", "不甘心", "沒進展", "没进展", "太天真", "還太理想",
              "想再", "下次", "明天再", "漏掉", "記錯", "卡住", "還沒", "还没",
              "再試", "再试", "半途", "差一點", "差一点")


def _looks_open(parts) -> bool:
    """這次活動是不是「還留著一條線」：看最後一兩句有沒有那些未完成的語氣。"""
    tail = [str(item) for item in (parts or ())[-2:] if str(item).strip()]
    return any(mark in text for text in tail for mark in OPEN_MARKS)


#: 「這一步沒有工作迴路」的留痕：每個角色只印一次，免得刷滿日誌
_NO_WORKER_WARNED: set[str] = set()

#: 連續幾步「沒有位移」就強制換做法（owner 2026-11-03 ④）；再幾步就收工。
#: 判詞由 `runtime/subagents.ProgressCritic` 給；判不了（超時／解析不到）一律放行。
STUCK_BRANCH = max(1, int(os.environ.get("BLUEAGENT_STUCK_BRANCH") or 3))
STUCK_ABORT = max(STUCK_BRANCH + 1, int(os.environ.get("BLUEAGENT_STUCK_ABORT") or 6))


def _warn_no_worker(activity) -> None:
    """`work` 與 `call` 都為空 ⇒ 這一步只會記失敗。

    從前這種情況**完全靜默**，日誌上只看到「（この一歩はできなかった：no_worker）」，
    分不出是「沒有 worker」還是「worker 拋錯」（owner 2026-10-27 要求查清）。
    """
    who = str(getattr(activity, "entity", "") or "?")
    if who in _NO_WORKER_WARNED:
        return
    _NO_WORKER_WARNED.add(who)
    print(f"  life[{who}]: 沒有工作迴路（work=None 且 call=None）⇒"
          f" 這一步只會記失敗；檢查 BLUEAGENT_SELF_WORK／LLM key／step_call", flush=True)


def _review_progress(activity, step_text: str, body: str, critic) -> None:
    """這一步做完之後問觀察者：**有沒有往前**（owner 2026-11-03 ④）。

    為什麼要有人看：她做事的時候沒有別人驗收，「每一步都寫得像做完了、其實在原地繞」
    是這種自主迴圈最常見的壞法。判定與處置都是程式：

      · 判 `moved=false` 累積到 `STUCK_BRANCH`（3）⇒ 下一步的 prompt 強制「換做法」；
      · 累積到 `STUCK_ABORT`（6）⇒ 收工（`meta["abort"]`，活動 FAILED、台帳記一次失敗）；
      · 觀察者壞了／超時／看不懂 ⇒ **放行**（誤殺比放過更糟）。
    """
    meta = getattr(activity, "meta", None)
    if critic is None or not isinstance(meta, dict):
        return
    marks = meta.get("progress_marks")
    if not isinstance(marks, list):
        marks = []
        meta["progress_marks"] = marks
    try:
        verdict = critic.review(activity=str(getattr(activity, "title", "") or ""),
                                step=str(step_text or ""), result=str(body or ""),
                                marks=[str(item.get("why") or "") for item in marks[-3:]
                                       if isinstance(item, dict)])
    except Exception as exc:                        # noqa: BLE001 - 觀察者壞了不擋她做事
        print(f"  life[{getattr(activity, 'entity', '?')}]: 位移判定失敗"
              f"（{type(exc).__name__}）", flush=True)
        return
    moved = bool(verdict.get("moved", True))
    why = str(verdict.get("why") or "")[:60]
    marks.append({"at": round(time.time(), 3), "step": str(step_text or "")[:40],
                  "moved": moved, "why": why})
    del marks[:-5]                                  # 只留最近 5 條（落盤，重啟還知道卡在哪）
    if moved:
        meta["stuck"] = 0
        return
    stuck = int(meta.get("stuck") or 0) + 1
    meta["stuck"] = stuck
    print(f"  life[{getattr(activity, 'entity', '?')}]: 這一步沒有往前"
          f"（{stuck} 次・{why[:30]}）", flush=True)
    if stuck >= STUCK_ABORT:
        meta["abort"] = {"kind": "goal_invalid", "failures": stuck,
                         "step": str(step_text or "")[:60]}


def _history_lines(activity, collected: dict) -> list[str]:
    """前面幾步做過什麼（含「上一次這套還開著的部分」）。"""
    done = collected.get(str(activity.id)) or []
    history = [f"{step_name} → {result}" for step_name, result in done]
    carried = str((getattr(activity, "meta", {}) or {}).get("continuation") or "").strip()
    if carried:
        history = [f"（前回この件はここまで：「{carried}」）"] + history
    return history


def _reminder_hint(activity) -> str:
    """預定時刻到了（她自己排的）：提醒一句，讓她知道「這是約好的事」。"""
    meta = getattr(activity, "meta", {}) or {}
    title = str(meta.get("reminder") or "").strip()
    if not title:
        return ""
    at = str(meta.get("reminder_at") or "").strip()
    return (f"\n［予定の時刻{('（' + at + '）') if at else ''}：{title}］"
            "これは自分で決めておいた予定。まずこれをやる。")

def _failure_hint(activity) -> str:
    """上一次是怎麼失敗的 ⇒ 這一次要換做法（owner 2026-11-03 失敗三分法）。

    三件事按嚴重度疊起來：
      · `failure_kind` 給的「換入口／換做法」（`inner/failure.hint_for`）；
      · `blocked_routes`——走不通的那幾條路的名字（不寫出來，她會照樣往牆上撞）；
      · 失敗 ≥2 次 ⇒ 先把「為什麼失敗」寫成技能（owner 2026-11-03 S2）。
    """
    meta = getattr(activity, "meta", {}) or {}
    failures = int(meta.get("failures") or 0)
    kind = str(meta.get("failure_kind") or "")
    blocked = list(meta.get("blocked_routes") or ())
    parts = []
    from agent.brain.runtime.inner.failure import hint_for
    if kind in ("no_source", "wrong_path"):
        parts.append(hint_for(kind, blocked=blocked))
    elif blocked:
        # 沒有分類詞（舊存檔／這次是新的一輪）也要讓她知道哪些路已經走過
        parts.append("\n・もう通らなかった手：" + "／".join(str(item) for item in blocked[:3]))
    # 連續幾步「沒有往前」（觀察者判的）：不是失敗，但同樣要換做法（owner 2026-11-03 ④）
    if int(meta.get("stuck") or 0) >= STUCK_BRANCH and kind != "wrong_path":
        parts.append(hint_for("wrong_path", blocked=blocked))
    if failures >= 2:
        parts.append(f"\n［この件は {failures} 回うまくいっていない］"
                     "まず `mcp_skills_write` で『なぜ失敗したか／次はどうするか』を技能として殘す"
                     "（完了条件と失敗時を必ず書く）。それから本題に入る。")
    text = "".join(part for part in parts if part)
    if text and not text.startswith("\n"):
        text = "\n" + text
    return text


def _content_of(activity: Activity, name: str, text: str, *, work=None, call=None,
               persona_hint: str = "", history=(), progress: str = "",
               public: bool = False) -> tuple[str, list, list, str]:
    """真的做事：有 `work`（帶工具的迴圈）就用它，否則退回純文字呼叫。

    回 `(正文, 查證用的 trace, 實際叫過的 trace, 沒做成的理由)`：
      · 查證 trace 只在工作成功時有（決定筆記蓋「工具」章還是「憑印象」）；
      · 實際叫過的 trace **失敗也照回**——請求已經打出去了，網路費要照算
        （review.md 2026-10-26 第 2 條：以前失敗路徑回空 trace ⇒ 查不到就不扣費）；
      · **沒做成時正文回空字串＋理由**：以前把「步驟指示」當結果寫進日誌，
        公開流就出現「查這週有什麼新作」這種像計畫又像結果的句子（owner 2026-10-27）；
      · `history`：這一件事前面幾步做過什麼（`["步驟 → 結果", …]`）——沒有它，
        第③步「把那一條重寫成一句」根本不知道「那一條」是哪一條。
    """
    if work is None and call is None:
        _warn_no_worker(activity)
    brief = str((getattr(activity, "meta", {}) or {}).get("brief") or "")
    # 斷點續做（owner 2026-11-03）：主人打斷過她 ⇒ 讓她知道自己從哪裡接回來
    resume = (getattr(activity, "meta", {}) or {}).get("resume") or {}
    if isinstance(resume, dict) and resume:
        last = str(resume.get("last") or "")[:80]
        progress = (f"{str(progress or '').rstrip('。')}"
                    "・前回は話しかけられて中断した所から続き"
                    + (f"（前回ここまで：{last}）" if last else ""))
    past = [str(item) for item in (history or ()) if str(item).strip()]
    # 落檔的步驟（`step.note`）：**重啟後仍在**。記憶體那份 `collected` 一重啟就空，
    # 以前她因此只知道自己前面做到了哪一步都不知道（owner 2026-11-03）。
    if not past:
        past = [f"{item.title} → {item.note}" for item in getattr(activity, "steps", ())
                if str(getattr(item, "note", "") or "").strip()][-4:]
    attempted: list = []
    reason = "no_worker"
    if work is not None:
        try:
            # 舊的 `work` 只有 (title, step, entity=) ⇒ 別硬塞新關鍵字給它
            # （2026-10-24 實測：多傳一個關鍵字，整步 TypeError 而失敗）
            payload = {"entity": str(activity.entity)}
            if _accepts_keyword(work, "brief"):
                payload["brief"] = brief
            if _accepts_keyword(work, "form"):
                payload["form"] = str((getattr(activity, "meta", {}) or {}).get("form")
                                      or "note")
            if _accepts_keyword(work, "history"):
                payload["history"] = past
            if _accepts_keyword(work, "progress"):
                payload["progress"] = str(progress or "")
            if _accepts_keyword(work, "public"):
                payload["public"] = bool(public)
            if _accepts_keyword(work, "use_alt"):
                # 技能整理／失敗復盤：長上下文＋雲端腦（owner 2026-11-03）
                preset_key = str((getattr(activity, "meta", {}) or {}).get("preset") or "")
                failures_now = int((getattr(activity, "meta", {}) or {}).get("failures") or 0)
                payload["use_alt"] = (preset_key == "skills-tidy" or failures_now >= 2)
            result = work(activity.title, text, **payload) or {}
        except Exception as exc:                    # noqa: BLE001 - 做不動不擋活動
            print(f"  life[{activity.entity}]: 工作迴圈失敗（{type(exc).__name__}）", flush=True)
            result = {}
        attempted = list(result.get("trace") or [])
        body = str(result.get("text") or "").strip()
        if result.get("ok") and body:
            # 作品型可以長（300 字場景／表格），記事型 400 → 600 字（owner 2026-11-03：
            # max_tokens 本來就給到 1200，400 字是自己截掉的；「まとめる」類步驟寫不完）
            cap = 2000 if str((getattr(activity, "meta", {}) or {}).get("form")) == "work" \
                else 1000
            return body[:cap], attempted, attempted, ""
        reason = str(result.get("reason") or "no_result")
    if call is None:
        return "", [], attempted, reason
    prompt = STEP_PROMPT.format(persona=persona_hint or name,
                                activity=activity.title, step=text)
    if str((getattr(activity, "meta", {}) or {}).get("form") or "note") == "work":
        # 作品是**要留下來的成品**不是說明（owner 2026-11-08：「她发过来的不是表，只是表的说明」）
        prompt += ("\n※ これは作品として残る。説明ではなく**現物**を書く：表なら Markdown の表"
                   "（`| 欄 | 意味 | 記入例 |` の行）として実際に並べる。"
                   "「欄は〜である」という説明や箇条書きで済ませない。\n")
    if public:
        from agent.brain.runtime.self_work import PUBLIC_NOTE
        prompt += PUBLIC_NOTE
    if past:
        prompt += ("\n\n［この件でこれまでにやった部分（つながりを保つ。済んだことを繰り返さない、指示を言い直さない）］\n"
                   + "\n".join(f"・{item}" for item in past[-4:]) + "\n")
    try:
        body = str(call(prompt) or "").strip()
    except Exception as exc:                        # noqa: BLE001 - 做不動不擋活動
        print(f"  life[{activity.entity}]: 這一步沒做成（{type(exc).__name__}）", flush=True)
        return "", [], attempted, f"call:{type(exc).__name__}"
    return body[:400], [], attempted, ("" if body else "empty")


def _step_facts_on() -> bool:
    """每一步要不要寫進記憶（預設**不要**；owner 2026-11-03 降採樣）。"""
    return str(os.environ.get("BLUEAGENT_LIFE_STEP_FACTS") or "0").strip().lower() \
        in {"1", "on", "true", "yes"}


def _topic_of(activity) -> str:
    """生活流的主題：preset key（穩定）優先，沒有就用活動標題。"""
    meta = getattr(activity, "meta", None) or {}
    return str(meta.get("preset") or getattr(activity, "title", "") or "")[:40]


def _write_note(notes_root, entity_id: str, title: str, step_text: str, body: str, *,
                stamp: str = "", visibility: str = PUBLIC, form: str = "note",
                index: int = 0) -> None:
    """落到 `data/notes/<entity>/<日期>.md`（可翻閱、可驗收）。

    `form="work"`（做小作品）多寫一份**成品檔**到 `data/works/<entity>/`——
    成品要能單獨拿出來看，不是埋在日記流裡的一段。
    段落標頭會標 `〔private〕`：`notes.read` 讀回來時預設跳過這些段（披露）。
    """
    if notes_root is None:
        return
    try:
        import datetime as _dt
        import pathlib as _path
        now = _dt.datetime.now()
        day = now.date().isoformat()
        clock = now.strftime("%H:%M")
        mark = str(stamp or "")
        tag = "　〔private〕" if str(visibility) != PUBLIC else ""
        folder = _path.Path(notes_root) / str(entity_id)
        folder.mkdir(parents=True, exist_ok=True)
        extra = ""
        if str(form) == "work":
            # 成品放 `data/works/<entity>/`（不是 notes 底下：成品要能單獨拿出來看）
            works = _path.Path(notes_root).parent / "works" / str(entity_id)
            works.mkdir(parents=True, exist_ok=True)
            # ★ 2026-10-31（P3）：檔名用「日期＋活動」算，不是「日期＋時分＋序號」。
            #   以前同一件作品一天重做 40 次就留下 40 份差不多的檔（實測一天 79–144 份）。
            # ★ 2026-11-08：**一件作品一個檔**，步驟是檔裡的節（以前每步一個檔 ⇒
            #   「観測表」被拆成三份，主人收到的是「表的說明」而不是表）。
            def _slug(text: str) -> str:
                return "".join(ch if ch.isalnum() or ch in "-_" else "-"
                               for ch in str(text))[:48].strip("-") or "work"
            work_path = works / f"{day}-{_slug(str(title))}.md"
            merge_section(work_path, title=str(title), step=str(step_text), body=body)
            adopt_owner(work_path, like=_path.Path(notes_root).parent)
            extra = f"　〔作品：{work_path.name}〕"
        head = f"## {clock} 「{title}」／{step_text}{mark}{tag}{extra}"
        note_path = folder.joinpath(f"{day}.md")
        with note_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n{head}\n{body}\n")
        adopt_owner(note_path, like=_path.Path(notes_root).parent)
    except Exception as exc:                        # noqa: BLE001
        print(f"  life[{entity_id}]: 筆記沒寫成（{type(exc).__name__}）", flush=True)


def _note_failure(activity, step_text: str, reason: str, attempted: list, *,
                  has_worker: bool) -> str:
    """失敗先分類再處置（owner 2026-11-03 ①），回要寫進日誌的那一句。

    以前只有一種結局——整件事卡住等人；可是「沒做成」其實是三件不同的事，
    處置也不同（換入口／換做法／收工）。判定與落盤都在 `inner.failure`，
    這裡只負責把結果寫成她看得懂的一句。
    """
    from agent.brain.runtime.inner.failure import classify, is_transient, record
    meta = getattr(activity, "meta", {}) or {}
    # 端點抖一下（模型逾時／連不上）不算她的失敗：照實記，但不累計、不升級
    transient = is_transient(reason, attempted)
    kind = classify(failed=True, reason=reason, attempted=attempted,
                    failures=int(meta.get("failures") or 0),
                    has_worker=has_worker, transient=transient)
    disposition = record(activity, kind, step_text=step_text, transient=transient)
    print(f"  life[{getattr(activity, 'entity', '?')}]: 這一步沒做成"
          f"（{kind}・{reason or 'no_result'}）⇒ "
          + ("收工（台帳記一次失敗）" if disposition == "abort" else "換做法繼續"), flush=True)
    # 沒做成**照實寫**，不把步驟指示冒充結果（owner 2026-10-27 第 3 條）
    return f"（この一歩はできなかった：{kind}／{reason or 'no_result'}）"


def _stamp_of(tools_used: list, *, work, form: str, failed: bool) -> str:
    """筆記後面那個「蓋章」：真的用了哪些工具／是不是憑印象寫的。

    2026-10-31（P2）：被「步內預算／重複呼叫」擋下來的**沒有真的用**，不要蓋成「用過」的章。
    """
    if tools_used:
        shown = [str((item or {}).get("name") or "") for item in tools_used
                 if str((item or {}).get("error") or "") not in _DENIED_CALLS]
        shown = [name for name in shown if name]
        if shown:
            return "　〔ツール：" + "、".join(shown) + "〕"
        if work is not None and form != "work" and not failed:
            return "　〔未検証：記憶から〕"
        return ""
    if work is not None and form != "work" and not failed:
        return "　〔未検証：記憶から〕"                  # C：沒調工具就不能假裝查過
    return ""



def _fiction_of(activity) -> int:
    """這一套產出的是**虛構作品**嗎（owner 2026-11-16：「算她做過的事，不算她經歷過的事」）。"""
    return int((getattr(activity, "meta", None) or {}).get("fiction") or 0)


def _note_fact(memory, activity, fact_text: str, now: float) -> None:
    """把一步的筆記寫成一條事實（房間＝她＋主人；作品標 fiction）。"""
    memory.add(fact_text, layer="self", label="她的筆記", self_owned=True,  # lang-ok: 存檔鍵
               entity=str(activity.entity), source="self_note",
               audience=self_room(str(activity.entity),
                                  scope=os.environ.get("BLUEAGENT_SELF_ROOM") or ""),
               kind="event", valid_from=now, valid_to=now + LIFE_TTL_DAYS * 86400,
               topic=_topic_of(activity), fiction=_fiction_of(activity))


def _summary_fact(memory, activity, name: str, summary: str, now: float) -> None:
    """整件活動的成果摘要（唯一進記憶的那條）。"""
    memory.add(f"{name}が「{activity.title}」のこの回で得たもの：{summary}",
               layer="self", label="她的成果", self_owned=True,  # lang-ok: 存檔鍵
               entity=str(activity.entity), source="activity_summary",
               audience=self_room(str(activity.entity),
                                  scope=os.environ.get("BLUEAGENT_SELF_ROOM") or ""),
               kind="event", valid_from=now, valid_to=now + 14 * 86400,
               topic=_topic_of(activity), fiction=_fiction_of(activity))


def make_step_fn(memory: FactSinkPort, entities: EntityRegistry,
                 duration: float = 0.65, *, log: ActivityLog | None = None,
                 on_public=None, affect=None, call=None,
                 notes_root=None, persona_hint: str = "", work=None,
                 budget=None, net_cost: float | None = None, on_finish=None,
                 critic=None):
    """一步：睡一小會兒 → 寫日誌（private/public）→ public 那幾則同時沉進記憶。

    `on_public(entity, text, activity)` 是給「主動消息」用的鉤子（做完一件公開的事，
    她也許想說一句）；沒接就只是記錄。

    `on_finish(activity, close=..., is_open=...)`：一件活動收尾時回報最後一句與
    「是不是還開著」——跨活動延續就靠這個（owner 2026-10-27 第 4 條）。

    `budget` 給了就**按真的用到的工具加收網路費**（`BLUEAGENT_BUDGET_NET_COST`，
    每用一次 web.* 收這筆；owner 2026-10-25「調高使用網路的體力消耗」）。

    `critic`（owner 2026-11-03 ④）：只讀的位移觀察者（`subagents.ProgressCritic`）。
    給了就在每一步成功之後問一句「這一步有沒有往前」；沒有就是純粹不做判定。
    """

    #: 這個活動目前為止各步的（步驟名, 結果）
    collected: dict[str, list[tuple[str, str]]] = {}
    verified: dict[str, list[str]] = {}    # 同上，但沒調工具的那步前面帶「未検証」（只給收尾摘要用）

    def _record_step(activity, *, index: int, total: int, step_text: str,
                     visibility: str, body: str, failed: bool, tools_used: list,
                     stamp: str, form: str, name: str) -> str:
        """寫日誌／筆記／記憶／心情／主動通知；回這一格的結論字串。

        從 `step_fn` 抽出來：那支負責「做出內容」，這支負責「留下痕跡」
        （函式長度棘輪；語意也比較乾淨）。
        """
        # 日誌標出**進度與步驟名**：一件事的四步在時間線上才讀得成一條線，
        # 而不是「同一件事完成了四次」（owner 2026-10-27 第 2 條）。
        counter = f"{index + 1}/{total} " if total > 1 else ""
        head = f"{name}「{activity.title}」{counter}"
        last = bool(total) and index + 1 >= total
        line = (f"{head}完了：{body}" if last else f"{head}{step_text}：{body}")
        duplicate = False
        if log is not None:
            entry = log.append(activity.entity, kind=activity.kind or "activity", text=line,
                               visibility=visibility, activity=activity.title,
                               step=index, activity_id=str(activity.id))
            duplicate = bool(isinstance(entry, dict) and entry.get("duplicate"))
        if duplicate:
            return f"再実行（この枠は記録済み）：{body[:80]}"
        # 沒做成的一步不算作品：不然「作品」清單裡會多一份只寫著「できなかった」的檔
        _write_note(notes_root, activity.entity, activity.title, step_text, body, stamp=stamp,
                    visibility=visibility, form=("note" if failed else form), index=index)
        collected.setdefault(str(activity.id), []).append((step_text, body))
        # 收尾摘要是唯一進記憶的那條 ⇒ 憑印象寫的那步要標「未検証」（放前面：摘要每段只取 60 字）
        verified.setdefault(str(activity.id), []).append(body if tools_used else f"（未検証：記憶から）{body}")
        # 降採樣（owner 2026-11-03）：**每一步都寫進記憶已經關掉**。
        # 實測：生活流水是對話事實的 ~30 倍，600 條檢索窗口 2.5 天就換一輪，
        # 對話記憶整片消失。現在只留「日誌」(上面已經寫了) 與「每件事收尾一條摘要」。
        # 要看回來：`BLUEAGENT_LIFE_STEP_FACTS=1`。
        if (visibility == PUBLIC and not failed and _step_facts_on()
                and (call is not None or work is not None)):
            try:
                now = time.time()
                fact_text = body if tools_used else f"{body}（未検証：記憶から）"
                _note_fact(memory, activity, fact_text, now)
            except Exception as exc:                       # noqa: BLE001
                print(f"  life[{activity.entity}]: 筆記沒進記憶（{type(exc).__name__}）",
                      flush=True)
        if (call is not None or work is not None) and (
                str(getattr(activity, "status", "") or "") == "done" or last):
            parts = [result for _step, result in (collected.get(str(activity.id)) or [])]
            marked = verified.pop(str(activity.id), None) or []  # 重啟後補不回前幾步 ⇒ 對不齊就用原文
            if parts:
                summary = " ／ ".join(part[:60] for part in (marked if len(marked) == len(parts) else parts)[:3])
                try:
                    _summary_fact(memory, activity, name, summary, time.time())
                except Exception:                          # noqa: BLE001
                    pass
            if on_finish is not None:
                try:
                    on_finish(activity, close=parts[-1] if parts else "",
                              is_open=_looks_open(parts), summary=" ／ ".join(parts))
                except Exception as exc:                   # noqa: BLE001 - 收尾記錄不擋活動
                    print(f"  life[{activity.entity}]: 收尾記錄失敗"
                          f"（{type(exc).__name__}）", flush=True)
            collected.pop(str(activity.id), None)
        if affect is not None:
            try:
                from agent.brain.runtime.inner.activity_affect import appraise_activity
                appraise_activity(affect, activity.entity, activity.kind or "ambient", step_text,
                                  finished=str(getattr(activity, "status", "") or "") == "done" or bool(last))
            except Exception:                              # noqa: BLE001 - 心情壞了不能擋活動
                pass
        if visibility == PUBLIC and on_public is not None:
            try:
                on_public(activity.entity, line, activity)
            except Exception:                              # noqa: BLE001 - 通知失敗不影響活動
                pass
        return f"完了：{body[:80]}"

    def step_fn(activity: Activity, step) -> str:
        entity = entities.get(activity.entity)
        name = entity.name if entity is not None else activity.entity
        text = str(getattr(step, "title", "") or getattr(step, "text", "") or "").strip()
        visibility = str(getattr(step, "visibility", "") or PUBLIC)
        try:
            index = activity.steps.index(step)
        except ValueError:
            index = int(getattr(activity, "cursor", 0) or 0)
        total = len(getattr(activity, "steps", ()) or ())
        # 前面幾步做過什麼（含「上一次這套還開著的部分」）：不給的話第③步不知道「那一條」是哪條
        history = _history_lines(activity, collected)
        # 進度：第幾步／共幾步＋（重啟後）上一次做到哪一步
        progress = f"{index + 1}/{total}ステップ目（この活動「{activity.title}」）"
        progress += _failure_hint(activity) + _reminder_hint(activity)
        body, tools_used, attempted, reason = _content_of(
            activity, name, text, work=work, call=call, persona_hint=persona_hint,
            history=history, progress=progress, public=(visibility == PUBLIC))

        _charge_network(activity, attempted, text, budget=budget, net_cost=net_cost)
        if call is None and work is None:
            time.sleep(max(0.0, float(duration)))          # 假模式仍保留節奏
        form = str((getattr(activity, "meta", {}) or {}).get("form") or "note")
        failed = not body
        if failed:
            body = _note_failure(activity, text, reason, attempted,
                                 has_worker=(work is not None or call is not None))
        elif critic is not None:
            # 做成了也可能是原地繞（owner 2026-11-03 ④）：請只讀的觀察者判「有沒有往前」。
            _review_progress(activity, text, body, critic)
        stamp = _stamp_of(tools_used, work=work, form=form, failed=failed)
        return _record_step(activity, index=index, total=total, step_text=text,
                            visibility=visibility, body=body, failed=failed,
                            tools_used=tools_used, stamp=stamp, form=form, name=name)

    return step_fn


