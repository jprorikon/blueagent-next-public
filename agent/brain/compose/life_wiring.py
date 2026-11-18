"""她自己的生活：每一步真的做事（帶工具的自主任務）、活動輪換、每個角色一條 LifeLoop。

從 `bootstrap.build()` 拆出來（2026-10-25 結構 review 第 3 階段）。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from agent.brain.domain.activity import StepStatus
from agent.brain.runtime.life import Rotation, build_activity, make_step_fn, preset_key_of
from agent.brain.runtime.life_loop import LifeLoop


def _log(message: str) -> None:
    print(f"  {message}", flush=True)


def make_activity_affect(affect):
    """做完一件公開的事 ⇒ 多一點「被看見」的開心（pleasure +0.04／+0.02）。**不在這裡主動匯報**
    （owner 2026-10-22）：要不要說話由心跳按心情決定。

    情感整理（2026-11-09）：以前是 +0.22／+0.12 走 `appraise()`——核心一律 SOURCE_NOT_FOUND，從沒生效；
    現在走 `outcome()` 真的會進心情，所以幅度收到跟一般活動同一個量級（每步的類型增量在 `activity_affect`）。"""

    def on_public(entity: str, text: str, activity) -> None:
        body = str(text or "").strip()
        if not body:
            return
        try:
            # 一件事只算一次（同一個 event_id 再來核心回 DUPLICATE）：公開的步驟可能有好幾步
            affect.outcome(entity, kind="pleasure", delta_v=0.04, delta_a=0.02,
                           event_id=f"public:{entity}:{getattr(activity, 'id', '') or int(time.time())}",
                           target_type="task", target_id=str(getattr(activity, "id", "") or entity))
        except Exception:                              # noqa: BLE001
            pass

    return on_public


def _skill_bodies(skills, who: str, *, limit: int = 6, chars: int = 600) -> str:
    """技能整理專用：把「她自己的」技能正文挑幾支放進 prompt（合併／去重需要看到內容）。"""
    try:
        rows = [skill for skill in skills.all(who) if skill.source == "entity"]
    except Exception:                                  # noqa: BLE001
        return ""
    if not rows:
        return ""
    rows = rows[:limit]
    blocks = [f"・{skill.name}（{skill.category}／{skill.status}）\n{skill.body[:chars]}"
              for skill in rows]
    return "### 自分の技能の中身（整理・統合用に最大 %d 件）\n%s" % (limit, "\n\n".join(blocks))


def make_self_work(*, brain, tools, souls, speak_lang, interests=None, activity_log=None,
                   ledger=None, skills=None, skills_brain=None, world=None, base=None):
    """她自己的事也要能查資料（owner 2026-10-24）：帶工具的真的工作迴圈。

    `interests`（owner 2026-11-03）：她自己那幾條**関心**——放進 persona 段落，
    她才會拿自己的興趣去查、去玩，而不是照著步驟文字空轉。
    這一段與「相手の領域」分開讀（`persona/interests.py`）：對方的興趣只做接話材料。
    """
    from agent.brain.runtime.self_work import run_step
    from agent.brain.runtime.tools import ToolContext
    from agent.brain.runtime.inner.timeframe import human_age

    def self_work(activity_title: str, step_title: str, *, entity: str = "",
                  brief: str = "", form: str = "note", history=(),
                  progress: str = "", use_alt: bool = False,
                  public: bool = False) -> dict:
        who = str(entity or "mira")

        # 自主任務的工具面＝`self_work.SELF_WORK_TOOLS` 白名單；這裡不給委任能力、
        # 不給語音（她自己做事時不該撥電話），也不帶主人在場（產出會進 public 日誌）
        def context():
            return ToolContext(entity=who, principal="owner",
                               thread_id=f"self:{who}", source="self", voice=None,
                               mode="work", is_owner=True,
                               capabilities=frozenset(),
                               present=frozenset({who}))

        # 她自己做事**沒有回話延遲壓力**（owner 2026-11-03）：整份人格都給她，
        # 不要只給前 200 字——那等於每次做事都失憶自己是誰。関心接在後面。
        # 深上下文 v2（owner 2026-11-03「这些自我活动应该会需要更长的上下文空间」）：
        # 人格整份（soul 約 6.5k 字）——以前 [:2000] 會把角色檔後半切掉，等於每次做事都少一段自己。
        persona = souls.block(who)[:6000] if souls is not None else ""
        hint = interests.block(who) if interests is not None else ""
        if hint:
            persona = f"{persona}\n\n{hint}" if persona else hint
        # 最近的自己（公開日誌最近幾條）：讓她接得上「前幾天在弄什麼」。
        # 只用 public —— 這段會影響公開產出，私記照紀律不進來。
        recent = ""
        if activity_log is not None:
            try:
                rows = activity_log.entries(who, limit=(24 if use_alt else 12),
                                            visibility="public")
                lines = [f"-（{human_age(time.time() - float(row.get('at') or 0))}）"
                         f"{str(row.get('text') or '')[:90]}"
                         for row in (rows or ()) if str(row.get("text") or "").strip()]
                if lines:
                    recent = ("### 最近の自分（公開の記録）\n" + "\n".join(lines))
            except Exception:                          # noqa: BLE001 - 取不到就不給
                recent = ""
        # 自己的技能樹（owner 2026-11-03）：寫新技能之前先看見分支，避免重複造一支一樣的。
        if skills is not None:
            try:
                tree = skills.render_index(mode="work", entity=who, tool_available=None,
                                           per_group=(12 if use_alt else 4))
                if tree:
                    recent = f"{tree}\n\n{recent}".strip()
                if use_alt:
                    # 整理技能要「看得到葉子」才談得上合併去重 ⇒ 把幾支的正文也帶進來
                    recent = f"{_skill_bodies(skills, who)}\n\n{recent}".strip()
            except Exception:                          # noqa: BLE001 - 取不到就不給
                pass
        # 台帳摘要（owner 2026-11-03）：她要**有意識地**知道自己手上有哪些斷點／待辦，
        # 不是每一拍都當成「今天第一次動手」。
        board_obj = ledger.get(who) if isinstance(ledger, dict) else ledger
        if board_obj is not None:
            try:
                board = board_obj.summary()
                if board:
                    recent = f"{board}\n\n{recent}".strip()
            except Exception:                          # noqa: BLE001
                pass
        # 技能整理／技能學習這種長上下文步驟可以走另一顆腦（owner 2026-11-03）；
        # 「寫技能」的步驟一律日語化（他自己要求：口吻不重要，語言要對）。
        alt = skills_brain if (skills_brain is not None and use_alt) else None
        if alt is not None:
            step_title = f"{step_title}\n（技能の本文も必ず日本語で書く。中国語を混ぜない）"
        # 世界變化（owner 2026-11-03 ②）：距上一步多久、台帳變了什麼、這段期間有沒有人找她、
        # 她自己留下了什麼。放在最前面——她要先知道「時間過去了、外面動了」才開始想。
        delta = ""
        world_for = world.get(who) if isinstance(world, dict) else world
        if world_for is not None:
            try:
                # 這條路寫出來的是**公開**筆記 ⇒ 別人在房裡說的原話不給（只給「哪個房間、幾回」）
                delta = str(world_for.block(public=True) or "")
            except Exception:                          # noqa: BLE001 - 取不到就不給
                delta = ""
        if delta:
            recent = f"{delta}\n\n{recent}".strip()
        # 自傳式記憶（提案⑤，owner 2026-11-16）：她自己做事時看得到「我是怎樣的人／最近的我」，
        # 每行都帶 fact id；重建在 `wiring._start_self_model_worker`（每天一次，確定性）。
        try:
            from agent.brain.runtime.inner import self_facts
            mine = self_facts.self_block(base, who)
            if mine:
                recent = f"{mine}\n\n{recent}".strip()
        except Exception:                              # noqa: BLE001 - 取不到就不給
            pass
        return run_step(brain, tools, entity=who, activity=activity_title, step=step_title,
                        alt_brain=alt, alt_reason=("skills:deepseek" if alt is not None else ""),
                        long_context=bool(use_alt),
                        lang=speak_lang, brief=str(brief or ""), form=str(form or "note"),
                        history=history, progress=str(progress or ""), persona=persona,
                        context_block=recent, public=bool(public),
                        context_factory=context, log=_log)

    return self_work


@dataclass
class Lives:
    loops: dict
    step_fn: object
    rotation: object
    next_activity: object


def _wake_hint(ledger, idle_pause: float):
    """生活線的醒來提示：**只看這個角色自己的**台帳（2026-11-03 review：以前三本一起看 ⇒
    アスター有一件過期沒接的，三條生活線都每 60 秒醒一次）。回「最近的預定時刻還有多久（秒）」，
    沒有就照常睡滿 idle_pause。"""
    def _wake_in() -> float:
        moment = time.time()
        soonest = float(idle_pause)
        if ledger is None:
            return soonest
        for task in ledger.due_tasks(moment, within_s=0.0):
            soonest = min(soonest, max(60.0, float(task.get("due_at") or 0.0) - moment))
        for task in ledger.items():
            due = float(task.get("due_at") or 0.0)
            # 只算還能開工的（locked／做完的／過期的不該把她叫醒）
            if due > moment and str(task.get("state") or "") in ("pending", "hanging"):
                soonest = min(soonest, max(60.0, due - moment))
        return soonest
    return _wake_in


def _make_critic(work, step_call):
    """位移觀察者（owner 2026-11-03 ④）：只在「真的會做事」時才有意義。

    沒接工作迴路（MockBrain／測試）就不建——不然每一步都多一次沒用的 LLM 呼叫。
    `BLUEAGENT_PROGRESS_CRITIC=0` 可關掉；`BLUEAGENT_CRITIC_TIMEOUT_S` 預設 3 秒。
    """
    if work is None or step_call is None:
        return None
    if str(os.environ.get("BLUEAGENT_PROGRESS_CRITIC") or "1").strip().lower() \
            in {"0", "off", "false", "no"}:
        return None
    from agent.brain.runtime.subagents import ProgressCritic
    return ProgressCritic(step_call,
                          timeout_s=float(os.environ.get("BLUEAGENT_CRITIC_TIMEOUT_S") or 3.0))


def build_world_runtime(base, entities, ledgers, threads, *, locations, activity_log):
    """組合根一次拿到同一組觀察者與事件匯流排，避免各自建實例。"""
    from agent.brain.runtime.inner.world_events import WorldEvents
    events = WorldEvents()
    worlds = build_worlds(base, entities, ledgers, threads, locations=locations,
                          activity_log=activity_log, events=events)
    return worlds, events


def build_worlds(base, entities, ledgers=None, threads=None, *, locations=None,
                 activity_log=None, events=None):
    """共享環境一份＋每角色一份觀察者；私人房間事件由 WorldEvents 按範圍投遞。"""
    from agent.brain.runtime.inner.world_delta import (
        SharedWorld, WorldDelta, talked_from_threads)
    weather = None
    if locations is not None:
        from agent.adapter.weather import CachedWeather
        weather = CachedWeather(locations, base / "world" / "weather-cache.json")
    shared = SharedWorld(base / "world", threads=threads, weather=weather,
                         locations=locations, events=events,
                         interval_s=float(os.environ.get("BLUEAGENT_WORLD_SHARED_S") or 300.0))
    talked = talked_from_threads(threads)
    peers = tuple(entity.id for entity in entities.all())
    rows = {}
    for entity in entities.all():
        guild_id = str(os.environ.get(f"BLUEAGENT_DISCORD_{entity.id.upper()}_GUILD_ID")
                       or os.environ.get("GUILD_ID") or "")
        rows[entity.id] = WorldDelta(base / "world", entity.id,
                                     ledger=(ledgers or {}).get(entity.id),
                                     notes_root=base / "notes", talked=talked,
                                     shared=shared, events=events, activity_log=activity_log,
                                     peers=peers, guild_ids=(guild_id,) if guild_id else ())
    return rows


def make_finish_recorder(rotation, activity_log):
    """活動收尾的記錄器（輪換的延續／防自旋 ② 的產出比對／照實的私人一句）。"""

    def _finish_recorder(activity, *, close: str, is_open: bool, summary: str = "") -> None:
        """一件活動收尾 ⇒ 記下最後一句與「還開著嗎」（下次輪換優先接著做）；
        再跟同一套最近 3 次的產出比（防自旋 ②），**一樣就照實記一句私人的**（取代以前寫死的劇本情緒）。"""
        key = preset_key_of(activity)
        if not key:
            return
        rotation.note_close(activity.entity, key, close=close, is_open=is_open)
        state = rotation.note_output(activity.entity, key, summary or close)
        stale = int(state.get("stale") or 0)
        if stale and activity_log is not None:
            days = min(7, 2 ** (stale - 1))
            try:
                activity_log.append(activity.entity, kind="reflection", visibility="private",
                                    activity=str(getattr(activity, "id", "") or ""),
                                    text=f"「{activity.title}」、前と同じようなことをまとめただけだった。"
                                         f"このやり方は{days}日ほど置いておく")
            except Exception:                              # noqa: BLE001 - 日誌壞了不擋收尾
                pass

    return _finish_recorder


def make_rest_check(affect, activity_log):
    """回 `rather_rest(who) -> bool`：心情不好 ⇒ 這次先不開新活動（`runtime/inner/willing.py`）。"""

    def rather_rest(who: str) -> bool:
        """心情不好 ⇒ 這次先不開（記一句私人的，照實寫原因）。`BLUEAGENT_LIFE_MOOD_REST=0` 關。"""
        if affect is None or str(os.environ.get("BLUEAGENT_LIFE_MOOD_REST") or "1").strip().lower() \
                in {"0", "off", "false", "no"}:
            return False
        from agent.brain.runtime.inner import willing
        from agent.brain.runtime.inner.mood_cause import main_cause
        try:
            reason = willing.rest_reason(affect.mood(who), entity=who, now=time.time())
        except Exception:                              # noqa: BLE001 - 心情壞了就照常做
            return False
        if not reason:
            return False
        why = str(main_cause(affect, who).get("words") or "")
        print(f"  life[{who}]: 気が乗らない（{reason}）⇒ この回は休む", flush=True)
        if activity_log is not None:
            try:
                activity_log.append(who, kind="rest", visibility="private",
                                    text=willing.WORDS[reason] + (f"（{why}）" if why else ""))
            except Exception:                          # noqa: BLE001
                pass
        return True

    return rather_rest


def _wire_sleep_decay(affect, circadian) -> None:
    """睡眠时段放慢 AEM 衰减（owner 2026-11-16）：接在生物钟上，没接就是旧行为。"""
    if affect is None:
        return
    from agent.brain.runtime.inner import sleep_decay
    affect.time_scale = sleep_decay.from_clocks(circadian)


def build_lives(base, *, entities, memory, brain, tools, souls, speak_lang, activity_log,
                activity_store, affect, budget, step_call, step_duration, life_pause,
                ledgers=None, skills=None, skills_brain=None, threads=None,
                worlds=None, circadian=None, proactive=None) -> Lives:
    """每一步真的做事 → 活動輪換 → 每個角色一條 LifeLoop（沒有存檔才開一件新的）。"""
    from agent.brain.openai_compat import OpenAICompatibleBrain
    # 任務台帳（owner 2026-11-03）：動手之前先看自己手上還有什麼。
    # 由組合根傳進來（工具面也吃同一份，兩份實例會互相蓋掉狀態）；沒有就自己建。
    from agent.brain.runtime.inner.task_ledger import TaskLedger
    ledgers = ledgers or {entity.id: TaskLedger(base / "tasks", entity.id)
                          for entity in entities.all()}
    work_on = str(os.environ.get("BLUEAGENT_SELF_WORK") or "1").strip().lower() \
        not in {"0", "off", "false", "no"}
    # 只有「真的接上遠端大腦」才開工作迴圈：MockBrain／沒 key／測試環境一律不做，
    # 否則活動會在背景發真的 HTTP 請求（實測會把測試拖到逾時）。
    # 関心檔與人格檔同一層（`agent/persona/<entity>.interests.md`）⇒ 直接沿用 souls.root，不另立設定
    interests = None
    if souls is not None:
        try:
            from agent.brain.persona.interests import InterestsLibrary
            interests = InterestsLibrary(getattr(souls, "root", None))
        except Exception:                                  # noqa: BLE001 - 沒有関心檔也要能跑
            interests = None
    work = (make_self_work(brain=brain, tools=tools, souls=souls, speak_lang=speak_lang,
                          interests=interests, activity_log=activity_log, ledger=ledgers,
                          skills=skills, skills_brain=skills_brain, base=base,
                          world=(worlds if worlds is not None
                                 else build_worlds(base, entities, ledgers, threads)))
            if (work_on and isinstance(brain, OpenAICompatibleBrain) and step_call is not None)
            else None)
    # 開機留痕：工作迴路到底有沒有接上（owner 2026-10-27 查 no_worker：從前完全看不出）
    print(f"  life: 工作迴路={'on' if work else 'off'}"
          f"（SELF_WORK={str(os.environ.get('BLUEAGENT_SELF_WORK') or '1')}"
          f" brain={type(brain).__name__} step_call={'有' if step_call is not None else '無'}）",
          flush=True)
    # 活動輪換：按活動庫輪流挑，狀態落 `data/life-rotation.json`
    #
    # ★ 2026-10-31（owner「按順序做」的 P0）：以前歷史只留 24 筆（≈2 小時），
    #   「今天做過幾次」等於算不出來 ⇒ 同樣 5-7 套一天重跑約 55 輪、一天 ~1000 步。
    #   現在：① 歷史留 48 小時（`_prune`）；② 同一套一天最多 `BLUEAGENT_LIFE_DAILY_CAP` 次（預設 2）；
    #   ③ 兩次之間至少隔 `BLUEAGENT_LIFE_MIN_REPEAT_H` 小時（預設 3）；④ 全都排不進去 ⇒ 今天收工去休息。
    rotation = Rotation(base / "life-rotation.json",
                        daily_cap=int(os.environ.get("BLUEAGENT_LIFE_DAILY_CAP") or 2),
                        min_repeat_h=float(os.environ.get("BLUEAGENT_LIFE_MIN_REPEAT_H") or 3.0),
                        # 防自旋 ①：「主人跟她說過話」也算一手材料（落盤的那份，重啟不丟）
                        heard=(lambda who: proactive.heard_at(str(who)))
                        if proactive is not None and hasattr(proactive, "heard_at") else None)
    # 每步之間留白（P1）：以前 0.35 秒，實測 11 秒一步、一小時 39 步。
    # ⚠️ 真的接上遠端大腦（每步都花錢）⇒ 沒設環境變數也至少 60 秒；`life_pause`（build 預設 0.15）
    #    只給 MockBrain／測試用——以前寫成 `env or life_pause or 60.0`，60 永遠輪不到，沒走 compose 就照樣空轉。
    real_brain_pause = max(60.0, float(life_pause or 0.0)) if work is not None else life_pause
    step_pause = float(os.environ.get("BLUEAGENT_LIFE_STEP_PAUSE") or real_brain_pause)
    idle_pause = float(os.environ.get("BLUEAGENT_LIFE_IDLE_S") or 1800.0)

    _finish_recorder = make_finish_recorder(rotation, activity_log)

    step_fn = make_step_fn(memory, entities, step_duration, log=activity_log,
                           on_public=make_activity_affect(affect), call=step_call,
                           notes_root=base / "notes", work=work, budget=budget,
                           on_finish=_finish_recorder, critic=_make_critic(work, step_call))
    # 做完一件接著做下一件（換場時間 `BLUEAGENT_LIFE_GAP`，秒，預設 60）
    between = float(os.environ.get("BLUEAGENT_LIFE_GAP") or 60.0)

    def _activity_of_task(who: str, task: dict, rotation) -> object:
        """台帳上的任務 → 一份可執行的活動（續做的話接回斷點）。"""
        activity = build_activity(who, rotation=rotation, after=str(task.get("preset") or ""))
        if activity is None:                     # 輪換排不出來（今天做滿了）⇒ 別在這裡炸
            return None
        meta = activity.meta if isinstance(activity.meta, dict) else {}
        meta["task_id"] = str(task.get("id") or "")
        # 失敗次數（owner 2026-11-03 S2）：≥2 次時，那一步的 prompt 會要求她先寫技能再動手
        meta["failures"] = int(task.get("failures") or 0)
        # 預定時刻到了（owner 2026-11-03）：把「這是排好的事」帶進 prompt，她才知道是自己約的
        due_at = float(task.get("due_at") or 0.0)
        if due_at > 0.0 and due_at <= time.time() + 60:
            meta["reminder"] = str(task.get("title") or "")
            meta["reminder_at"] = time.strftime("%H:%M", time.localtime(due_at))
        cursor = int(task.get("cursor") or 0)
        if cursor > 0:
            # 續做：已完成的那幾步標成 done（`step.note` 補上「前回這裡」），
            # 不然她會把前面的步驟再做一遍。
            note = str(task.get("note") or "")
            for index, step in enumerate(activity.steps[:cursor]):
                step.status = StepStatus.DONE
                step.ended_at = time.time()
                step.note = note if index == cursor - 1 and note else "（前回ここまで）"
            activity.cursor = min(cursor, activity.total)
            meta["resume"] = {"cursor": cursor,
                              "next_step": (activity.current.title
                                            if activity.current is not None else ""),
                              "last": note, "reason": "hanging",
                              "saved_at": time.time()}
        return activity

    rather_rest = make_rest_check(affect, activity_log)

    def next_activity(who: str, previous=None):
        """**先查台帳**（續做 hanging → 撿 pending），台帳空了才從輪換新開一件。

        開之前先問她想不想做（`runtime/inner/willing.py`，owner 2026-11-09）：心情不好可以先不做。
        """
        if rather_rest(str(who)):
            return None
        ledger = ledgers.get(str(who))
        if ledger is not None:
            try:                                       # 主人留了批注 ⇒ 日誌留痕（她也會在 prompt 裡看到）
                unseen = ledger.unseen_comments()
                if unseen:
                    print(f"  life[{who}]: 主人の批注 {len(unseen)} 件（次の一手で自分で判断する）",
                          flush=True)
            except Exception:                          # noqa: BLE001
                pass
            task = ledger.next_task()
            if task is not None:
                print(f"  life[{who}]: 台帳撿到「{task.get('title')}」"
                      f"（{task.get('state')}）", flush=True)
                return _activity_of_task(str(who), task, rotation)
        # 台帳上沒有可做的 ⇒ 開一件新的，並登記成 pending（開工時轉 processing）
        activity = build_activity(who, rotation=rotation, after=preset_key_of(previous))
        if activity is None:
            return None
        if ledger is not None:
            task = ledger.add(activity.title,
                              preset=str((activity.meta or {}).get("preset") or ""))
            activity.meta["task_id"] = task["id"]
        return activity

    resume_beats = int(os.environ.get("BLUEAGENT_RESUME_BEATS") or 3)

    loops = {entity.id: LifeLoop(entity.id, activity_store, step_fn,
                                 step_pause=step_pause,
                                 next_activity=next_activity,
                                 between_pause=between,
                                 resume_beats=resume_beats,
                                 ledger=ledgers.get(entity.id),
                                 budget=budget,
                                 step_cost=float(os.environ.get("BLUEAGENT_BUDGET_STEP_COST")
                                                 or 1.0),
                                 idle_pause=idle_pause,
                                 wake_in=_wake_hint(ledgers.get(entity.id), idle_pause),
                                 circadian=(circadian or {}).get(entity.id),
                                 log=lambda message: print(message, flush=True))
             for entity in entities.all()}
    # 開機就各自在做事：沒有存檔才播一份自己的活動（有存檔就從斷點接，不覆蓋）
    for entity_id, loop in loops.items():
        first = next_activity(entity_id) if activity_store.load(entity_id) is None else None
        if first is not None:                  # 排不出來／先不做 ⇒ 迴圈自己休息到點再問
            loop.start(first)
    _wire_sleep_decay(affect, circadian)
    return Lives(loops=loops, step_fn=step_fn, rotation=rotation, next_activity=next_activity)


__all__ = ["Lives", "build_lives", "make_activity_affect", "make_self_work"]
