"""新一代 BlueAgent 的組裝根：只接線，不放業務邏輯。

    entities / principals / social
        → memory + brain + ports
        → pipeline（stages）
        → router + scheduler + runtime
        → voice / tools / activity life loop
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field

from agent.adapter.tts_bridge import build_tts
from agent.brain.compose import stance_seed, wiring
from agent.brain.compose.entities import build_entities, build_principals, build_social
from agent.brain.compose.heartbeat_compose import (heartbeat_prompt, heartbeat_system,  # noqa: F401
                                                  make_heartbeat_composer, heartbeat_extras)
from agent.brain.compose.inner_life import build_inner_life, make_usage_recorder
from agent.brain.compose.life_wiring import build_lives
from agent.brain.compose.pipeline import build_pipeline
from agent.brain.compose.summary import summarize
from agent.brain.factory import build_brain
from agent.brain.skills_brain import build_skills_brain
from agent.brain.domain.attention import AttentionConfig
from agent.brain.domain.episode import SegmentPolicy
from agent.brain.domain.event import SourceClass
from agent.brain.memory.fact_store import FactStore
from agent.brain.persona.soul import SoulLibrary
from agent.brain.router.policy import Router, RouterConfig
from agent.brain.runtime.activity_log import ActivityLog
from agent.brain.runtime.activity_store import ActivityStore
from agent.brain.runtime.inner.affect import AffectService
from agent.brain.runtime.attention import AttentionGate
from agent.brain.runtime.identity.auth import AuthService
from agent.brain.runtime.budget import ActionBudget
from agent.brain.runtime.delivery.bus import EventBus
from agent.brain.runtime.delivery.delivery import AppChannel, ChannelRouter
from agent.brain.runtime.entity_store import EntityStore
from agent.brain.runtime.episodes import DEFAULT_MAX_KEPT, EpisodeArchive
from agent.brain.runtime.identity.guest_store import GuestStore
from agent.brain.runtime.heartbeat import build_heartbeats
from agent.brain.runtime.ingress import IngressRuntime
from agent.brain.runtime.life import build_activity  # noqa: F401  (舊 import 照用)
from agent.brain.runtime.pending_store import PendingInterventionStore
from agent.brain.runtime.playground import Playground
from agent.brain.runtime.proactive import ProactiveGate
from agent.brain.runtime.scheduler import Scheduler, SchedulerConfig
from agent.brain.runtime.stance import StanceService
from agent.brain.runtime.stance_store import StanceStore
from agent.brain.runtime.ops.telemetry import TurnRecorder
from agent.brain.runtime.threadstore import ThreadStore
from agent.brain.runtime.tools import ToolRegistry
from agent.brain.runtime.voice import VoiceService

from agent.brain.compose.app_root import App  # noqa: E402,F401 - 組合根的型別在那支（E3）


def build_stt():
    """語音輸入：**與現役 blue agent 同一套**（SenseVoice + 名字修正＋可選 LLM 文本糾錯）。

    `BLUEAGENT_STT=off` ⇒ 不啟用（端點回 503）。引擎由 `BLUEAGENT_STT_ENGINE` 選
    （預設 sensevoice；沒裝 sherpa 或模型不在就自動退回 faster-whisper）。
    """
    mode = str(os.environ.get("BLUEAGENT_STT") or "local").strip().lower()
    if mode in {"off", "none", "0", "false", "no"}:
        return None
    from agent.adapter.end_judge import EndJudge, EndJudgeConfig
    from agent.adapter.stt_local import LocalSTT, SttConfig
    config = SttConfig.from_env(os.environ)
    log = lambda message: print(f"  {message}")          # noqa: E731
    # 「說完了沒」：預設跟文本糾錯走同一顆本地 qwen（沒設糾錯端點就不判斷）
    judge_config = EndJudgeConfig.from_env(os.environ, fallback_url=config.fix_url,
                                           fallback_model=config.fix_model)
    judge = None
    if judge_config is not None:
        from dataclasses import replace
        from agent.brain.config import llm as llm_config
        judge_config = replace(judge_config, extra_body=llm_config.thinking_payload(
            enable=False, base_url=judge_config.url))
        judge = EndJudge(judge_config, log=log)
    return LocalSTT(config, log=log, end_judge=judge)


def build_speech_hold():
    """通話的「沒講完先扣住」：`BLUEAGENT_CALL_HOLD_S`（預設 1.5 秒；0＝關）。"""
    from agent.brain.runtime.voice.speech_hold import SpeechHold
    try:
        hold_s = float(os.environ.get("BLUEAGENT_CALL_HOLD_S") or 1.5)
    except ValueError:
        hold_s = 1.5
    return SpeechHold(hold_s=hold_s)


def build_delegate(userfile=None):
    """委任服務：只有設了 `HERMES_API_KEY` 才啟用（沒設＝工具面不出現，行為與之前相同）。

    端點與金鑰刻意沿用現役橋的變數名（`HERMES_API_URL` / `HERMES_API_KEY`），
    所以 `BLUEAGENT_ENV_FILE` 指現役那份就能直接跑。
    """
    base_url = str(os.environ.get("HERMES_API_URL") or "http://127.0.0.1:18642").strip()
    api_key = str(os.environ.get("HERMES_API_KEY") or "").strip()
    if not base_url or not api_key:
        return None
    from agent.adapter.hermes_delegate import HermesGateway
    from agent.brain.runtime.delegate import DelegateService
    dirs = str(os.environ.get("BLUEAGENT_ARTIFACT_DIRS") or "").strip()
    artifact_dirs = [item for item in dirs.replace(",", ":").split(":") if item.strip()]
    gateway = HermesGateway(base_url, api_key,
                            timeout=float(os.environ.get("BLUEAGENT_DELEGATE_TIMEOUT") or 900),
                            approval=str(os.environ.get("BLUEAGENT_DELEGATE_APPROVAL") or "allow"))
    policy = (lambda principal: userfile.allows(principal, "委任")) if userfile else None
    return DelegateService(gateway, artifact_dirs=artifact_dirs, policy=policy,
                           wait_s=float(os.environ.get("BLUEAGENT_DELEGATE_WAIT_S") or 20),
                           deadline=float(os.environ.get("BLUEAGENT_DELEGATE_DEADLINE") or 900))


def _episodes_with_indexes(base, names, memory_search, threads) -> EpisodeArchive:
    """封片檔＋序號對帳（崩潰後 threads.json 可能落後封片檔，序號只往前）。"""
    archive = _episode_archive(base, names, memory_search)
    settled = archive.reconcile_indexes(threads.threads())
    print(f"  episodes: 序號對帳 {settled['bumped']} 條線（封片檔裡 {settled['threads']} 條）",
          flush=True)
    return archive


def _wire_heartbeats(*, entities, affect, proactive, delivery, activity_log, budget, reminders,
                     timeline, inner, memory_search, principals, souls, persona_cores, brain,
                     raw_log, speak_lang, tools_registry, base, lives=None, threads=None,
                     worlds=None, deliveries=None, voice=None):
    """心跳（每個角色一條）＋「時間感」與「幾跳之後提醒」的接線。"""
    beats = wiring.build_beats(base)              # 每一跳留一行（回指「第 N 跳」）
    # 心情定位（owner 2026-11-01 第①項）：v／a → 一句人話。**呼叫自己一條**（額度小），
    # 不與狀態引出共用（那條 1200 tokens，這條只要 300）。預設關，compose 裡 BLUEAGENT_MOODLINE=1 開。
    from agent.brain.persona.core import http_call
    from agent.brain.runtime.inner.moodline import MoodLine
    moodline = MoodLine(
        base / "mood",
        call=http_call(dict(os.environ),
                       max_tokens=int(os.environ.get("BLUEAGENT_MOODLINE_MAX_TOKENS") or 300),
                       disable_thinking=True, role="moodline"),
        log=lambda message: print(f"  {message}", flush=True))
    # 內心獨白（owner 2026-11-01「走下一步」）：節奏階梯 + 一拍一句 + 一個動作。
    # 消費方在心跳：`work` ⇒ 開一件自己的事；`talk` ⇒ **只當動機**交給組句器（原文不出門，
    # `BLUEAGENT_MONOLOGUE_TALK=raw` 才回舊行為；docs/PLAN-common-ground-20261109.md 第 1 步）。
    from agent.brain.runtime.inner.monologue import Monologue
    monologue = Monologue(
        base / "inner",
        call=http_call(dict(os.environ),
                       max_tokens=int(os.environ.get("BLUEAGENT_MONOLOGUE_MAX_TOKENS") or 700),
                       disable_thinking=True, role="monologue"),
        log=lambda message: print(f"  {message}", flush=True))
    # 獨白回 `action=call` ⇒ 她想打電話給主人（過主動消息的閘＋電話額度；打不出去就改用說的）
    from agent.adapter.app_call_out import self_dial
    from agent.brain.runtime.voice.outbound import CallWish
    monologue = CallWish(monologue, dial=self_dial(voice, principals.default()))
    compose = make_heartbeat_composer(
        brain=brain, souls=souls, persona_cores=persona_cores, activity_log=activity_log,
        affect=affect, prethinker=inner.prethinker, proactive=proactive, raw_log=raw_log,
        principals=principals, speak_lang=speak_lang,
        latest_window=lambda entity: memory_search.latest_window(str(entity),
                                                                 {principals.default()}),
        # 「主人剛剛說到哪」優先看當前活線的 transcript（raw 只當兜底，見 2026-11-01 那條）
        recent_dialogue=(lambda entity, limit=12: threads.recent_dialogue(str(entity), limit))
        if threads is not None else None,
        # 2026-11-08：主人的話**跨線**找（最近活線可能是角色↔角色那條，主人會被整段濾掉）
        recent_owner_lines=(lambda entity, limit=3: threads.recent_owner_lines(
            str(entity), limit,
            owners=tuple(p.id for p in principals.all() if getattr(p, "owner", False)) or ("owner",)))
        if threads is not None else None,
        deliveries=deliveries)
    def start_self_work(entity: str, row: dict) -> None:
        """獨白選了 `action=work` ⇒ 開一件她自己的事（實作在 `compose/self_work_start.py`）。"""
        start_hook.start_rotation(entity, row, lives=lives)

    from agent.brain.compose import self_work_start as start_hook

    def owner_talking(name: str) -> bool:
        """主人正在跟**這個角色**說話嗎？（心跳讓路的判據）

        `LifeLoop.human_active` 由 `turns._execute` 在回合外圍 `human_enter/exit` 維護，
        所以這裡讀到的就是「他的這一輪還在跑」。讀不到（測試／沒接 life）一律當沒人說話
        —— 讓路是優化，不能反過來把心跳卡死。
        """
        loop = lives.get(str(name)) if isinstance(lives, dict) else None
        if loop is None:
            return False
        try:
            return bool((loop.snapshot() or {}).get("human_active"))
        except Exception:                                  # noqa: BLE001
            return False

    heartbeat = build_heartbeats(
        [entity.id for entity in entities.all()],
        affect=affect, gate=proactive, publish=delivery.send, activity_log=activity_log,
        compose=compose, budget=budget, reminders=reminders, beats=beats, moodline=moodline,
        monologue=monologue, on_self_work=start_self_work,
        drive_base=str(base),
        on_drive_work=lambda entity, signal: start_hook.start_drive(entity, signal, lives=lives,
                                                                    affect=affect),
        drive_running=lambda entity, activity_id: start_hook.drive_running(entity, activity_id,
                                                                           lives=lives),
        world=worlds,
        busy=owner_talking,
        **heartbeat_extras(threads=threads, proactive=proactive, principals=principals,
                           log=lambda message: print(f"  {message}", flush=True)),
        quiet_s=float(os.environ.get("BLUEAGENT_HEARTBEAT_QUIET_S") or 8.0),
        threshold=float(os.environ.get("BLUEAGENT_HEARTBEAT_THRESHOLD") or 0.35),
        log=lambda message: print(f"  {message}", flush=True))
    timeline.heartbeats = heartbeat
    wiring.register_remind_tool(tools_registry, reminders, heartbeat)
    return heartbeat


def _episode_archive(base, names, memory_search) -> EpisodeArchive:
    """封片檔：承接段用「提煉窗口摘要」（只接同一條線），她換段／換世代時要接得上。

    `max_kept` 是**記憶體裡留幾筆**：舊值 512 會把歷史擠出去（2026-10-27 實測檔案裡
    564 筆、裁到 512 就少 52 筆）⇒ 預設 `DEFAULT_MAX_KEPT`（2048），可用環境變數調。
    """
    return EpisodeArchive(
        policy=SegmentPolicy(idle_gap_s=90.0, max_turns=6, max_chars=1600),
        summarize=lambda episode: summarize(episode, names), path=base / "episodes.jsonl",
        window_summary=memory_search.carry_window,
        max_kept=int(os.environ.get("BLUEAGENT_EPISODES_MAX") or DEFAULT_MAX_KEPT),
    )


@dataclass
class _LocalServices:
    """組合根內部的一組本機服務；不是對外 service locator。"""
    voice: VoiceService
    stt: object
    userfile: object
    delegate: object
    stance_store: StanceStore
    stances: StanceService
    playground: Playground
    locations: object
    activity_log: ActivityLog
    proactive: ProactiveGate
    #: 預算（閘）；開了存摺之後是 `LedgerMirror`——介面同 `ActionBudget`，多一筆記帳
    budget: object
    affect: AffectService
    #: 各自的生物鐘（owner 2026-11-08）：`{entity: CircadianClock}`；空＝這版沒有。
    circadian: dict = field(default_factory=dict)


def _wire_mood_events(bus, affect, principal: str) -> None:
    from agent.brain.runtime.inner.mood_events import MoodEvents
    affect.on_change = MoodEvents(bus, affect, principal=principal).publish


def _build_local_services(*, base, entities, principals, memory) -> _LocalServices:
    voice = VoiceService(entities, tts=build_tts())
    stt = build_stt()
    # user.md 每輪進 prompt；溢出內容沉進本人可見的私密記憶。
    from agent.brain.persona.userfile import UserFile
    userfile = UserFile(base / "users", host=principals.default(),
                        on_overflow=lambda who, items: [
                            memory.add(item, layer="private", audience={who},
                                       principal=who, source="userfile_overflow")
                            for item in items])
    userfile.ensure("owner", name="主人", call_name="オーナー")
    delegate = build_delegate(userfile=userfile)
    stance_store = StanceStore(base / "stances")
    stances = StanceService(stance_store, stance_seed.build_catalog())
    stance_seed.seed(stances)

    playground = Playground(base / "playground")
    for entity in entities.all():
        playground.resolve(entity.id, "own")
    playground.resolve("", "shared")
    from agent.brain.runtime.locations import LocationStore
    from agent.adapter.app_locations import build_location_store
    locations = build_location_store(LocationStore(base / "locations"))
    activity_log = ActivityLog(base / "logs", public_sink=wiring.make_public_sink(memory))
    from agent.brain.compose.boot_wiring import build_local_core   # 生物鐘＋閘＋預算＋情緒
    core = build_local_core(base, entities, log=lambda message: print(message, flush=True))
    circadian, proactive, budget, affect = core.circadian, core.proactive, core.budget, core.affect
    return _LocalServices(voice, stt, userfile, delegate, stance_store, stances, playground,
                          locations, activity_log, proactive, budget, affect, circadian)


@dataclass
class _Cognition:
    """人格、原文、檢索與內心服务的装配結果。"""
    side: object
    speak_lang: str
    soul_dir: str
    souls: SoulLibrary
    prompt_stats: dict
    persona_cores: dict
    raw_log: object
    search: object
    skills: object
    inner: object


def _attach_prethink(prethinker, local, principals, entity_names) -> None:
    """預想（2026-11-09 檢查）：睡著不想、預算不夠不想、逐字稿寫名字——生物鐘與預算建好後才接得上。"""
    clocks = local.circadian or {}
    prethinker.attach(
        asleep=lambda who, now: bool(clocks.get(who)) and clocks[who].asleep(now), budget=local.budget,
        host=principals.default(), names={**{p.id: p.name for p in principals.all()}, **entity_names})


def _call_pickup(judge):
    """App 來電「接不接」：規則＋（有跡象在忙時）短呼叫判斷，見 `runtime/inner/call_pickup.py`。"""
    from agent.brain.runtime.inner.call_pickup import CallPickup
    return CallPickup(judge=judge, log=lambda message: print(f"  {message}", flush=True))


def _build_cognition(*, base, entities, principals, memory, activity_log,
                     entity_ids) -> _Cognition:
    side = wiring.build_side_calls()
    speak_lang = str(os.environ.get("BLUEAGENT_SPEAK_LANG") or "").strip().lower()
    soul_dir = str(os.environ.get("BLUEAGENT_SOUL_DIR")
                   or (pathlib.Path(__file__).resolve().parents[3] / "agent" / "persona"))
    prompt_stats: dict = {}
    from agent.brain.persona.core import load_core
    persona_cores: dict = {}
    souls = SoulLibrary(soul_dir)
    for found in entities.all():
        core = load_core(base / "persona", found.id, soul_text=souls.block(found.id))
        if core:
            persona_cores[found.id] = core

    from agent.brain.runtime.rawlog import RawLog
    raw_log = RawLog(base / "raw",
                     keep_months=int(os.environ.get("BLUEAGENT_RAW_KEEP_MONTHS") or 12))
    try:
        raw_log.rotate()
    except Exception:                          # noqa: BLE001 - 清舊檔失敗不擋啟動
        pass
    search = wiring.build_search(base, memory=memory, raw_log=raw_log, entity_ids=entity_ids)
    from agent.brain.runtime.skills import SkillLibrary
    skills = SkillLibrary(pathlib.Path(__file__).resolve().parents[1] / "skills",
                          entity_root=base / "skills", max_index=24)
    inner = build_inner_life(base, memory=memory, memory_search=search.memory_search,
                             raw_log=raw_log, activity_log=activity_log, principals=principals,
                             entity_ids=entity_ids, side=side)
    return _Cognition(side, speak_lang, soul_dir, souls, prompt_stats, persona_cores,
                      raw_log, search, skills, inner)


def build(*, brain_delay: float = 0.35, data_dir=None, step_duration: float = 0.65,
          life_pause: float = 0.15,
          attention_config: AttentionConfig | None = None) -> App:
    """照順序把各段接起來；每一段只負責一個裝配階段。"""
    entities = build_entities()
    principals = build_principals()
    social = build_social()
    env_dir = str(os.environ.get("BLUEAGENT_DATA_DIR") or "").strip()
    base = pathlib.Path(data_dir) if data_dir is not None else pathlib.Path(
        env_dir or (pathlib.Path(__file__).resolve().parents[3] / "data"))
    names = principals.names()
    entity_ids = [entity.id for entity in entities.all()]
    entity_names = {entity.id: str(getattr(entity, "name", entity.id)) for entity in entities.all()}

    memory = wiring.build_memory(base, store=FactStore(base / "memory" / "facts.jsonl"),
                                 names=names, social=social, entity_ids=entity_ids,
                                 entity_names=entity_names)
    brain = build_brain(entities, principals, delay=brain_delay)
    skills_brain = build_skills_brain(entities, principals)
    local = _build_local_services(base=base, entities=entities, principals=principals,
                                  memory=memory)
    cognition = _build_cognition(base=base, entities=entities, principals=principals,
                                 memory=memory, activity_log=local.activity_log, entity_ids=entity_ids)
    memory_search = cognition.search.memory_search
    memory_search.stance = local.stances        # 注意力場 β 項：她→這個人的關係強度（2026-11-16）
    _attach_prethink(cognition.inner.prethinker, local, principals, entity_names)
    tools_registry = ToolRegistry()
    # C-17：素材属于客户端；Agent 只经 App Server 取 ID／meaning，旧端点按需取单张图。
    from agent.adapter.app_assets import build_sticker_catalog
    stickers = build_sticker_catalog(data_dir=base)
    # 動作片段庫（`docs/PLAN-motion-kimodo-20261031.md` §5.1）：`$BLUEAGENT_DATA_DIR/motion/<id>.json`。
    # 空庫＝這一版不發 `motion` 幀（端末照舊；生成在 GPU 機，不在這台）。
    from agent.brain.compose.motion import MotionPlanner
    from agent.brain.media.motion import MotionLibrary  # noqa: E402
    motion_library = MotionLibrary(base / "motion")
    motion = MotionPlanner(motion_library, enabled=str(
        os.environ.get("BLUEAGENT_MOTION") or "on").strip().lower()
        not in {"off", "0", "false", "no"})
    timeline, reminders = wiring.build_timeline(local.activity_log), wiring.build_reminders(base)
    # 現況倉／任務台帳／未渡しの依頼（owner 2026-11-08：交付才清）——各自一份檔案，見 wiring。
    activity_store, ledgers, deliveries, files_store = wiring.build_stores(base, entities)
    media_store = wiring.register_tools(
        tools_registry, base=base, entities=entities, delegate=local.delegate,
        playground=local.playground, locations=local.locations,
        activity_log=local.activity_log, memory_search=memory_search, skills=cognition.skills,
        stickers=stickers, files=files_store)
    threads = ThreadStore(
        path=base / "threads.json",
        max_segment=int(os.environ.get("BLUEAGENT_THREAD_SEGMENT_MAX") or 200),
        max_state_bytes=int(os.environ.get("BLUEAGENT_THREAD_STATE_MAX_BYTES") or 8 * 1024 * 1024),
        archive_dir=base / "archive", raw_log=cognition.raw_log)
    pipelines, ports = build_pipeline(
        entities, principals, social, memory, brain, local.voice, tools_registry, local.stances,
        memory_usage=make_usage_recorder(memory), speak_lang=cognition.speak_lang,
        soul=SoulLibrary(cognition.soul_dir), userfile=local.userfile,
        activity_log=local.activity_log, affect=local.affect,
        prompt_stats=cognition.prompt_stats, persona_cores=cognition.persona_cores,
        llm_call=cognition.side.elicit, orchestrator=cognition.inner.orchestrator,
        prethinker=cognition.inner.prethinker, skills=cognition.skills, timeline=timeline,
        log=lambda message: print(f"  {message}", flush=True),
        log_path=base / "logs" / "state-elicit.jsonl", motion=motion, search=memory_search,
        activity_store=activity_store, deliveries=deliveries, elsewhere=threads.elsewhere)
    entity_store = EntityStore()
    scheduler = Scheduler(SchedulerConfig(realtime_slots=2, interactive_slots=2, ambient_slots=1),
                          name="blueagent-next", entities=len(entities.all()))
    archive = _episodes_with_indexes(base, names, memory_search, threads)
    timeline.archive = archive
    wiring.seal_on_supersede(threads, archive)
    router_config = RouterConfig(
        wake_words=("ミラ", "mira", "アスター", "aster", "ノリ", "ノリ", "nori"),
        reply_to_group=False)
    deadline_override = str(os.environ.get("BLUEAGENT_DEADLINE_MS") or "").strip()
    if deadline_override:
        router_config.deadlines[SourceClass.INTERACTIVE] = int(deadline_override)
    router = Router(threads, entities=entities, pipelines=pipelines.ids(), config=router_config)
    guest_store = GuestStore(base / "guests.json")
    from agent.brain.runtime.identity.accounts import AccountStore
    accounts = AccountStore(base / "accounts.json",
                            log=lambda message: print(f"  {message}", flush=True))
    auth = AuthService(base / "auth.json", principals, guest_store,
                       owner_principal=principals.default(), accounts=accounts)
    wiring.restore_guests(guest_store, principals=principals, social=social,
                          stances=local.stances, entities=entities)
    attention = AttentionGate(attention_config, lease_path=base / "leases.json",
                              log_path=base / "attention.jsonl")
    runtime = IngressRuntime(router=router, threads=threads, pipelines=pipelines,
                             scheduler=scheduler, episodes=archive, entities=entity_store,
                             ports=ports, attention=attention)
    from agent.brain.runtime.delivery.outbox import TurnOutbox
    outbox = TurnOutbox(
        base / "outbox", keep=int(os.environ.get("BLUEAGENT_OUTBOX_KEEP") or 400),
        audio_bytes=int(float(os.environ.get("BLUEAGENT_OUTBOX_AUDIO_MB") or 96) * 1024 * 1024))
    pending_store = PendingInterventionStore(base / "pending.json")
    # 同一組世界實例給生活環與心跳，避免兩邊各建一份、狀態互相蓋掉。
    from agent.brain.compose.life_wiring import build_world_runtime
    worlds, world_events = build_world_runtime(
        base, entities, ledgers, threads, locations=local.locations, activity_log=local.activity_log)
    lives = build_lives(base, entities=entities, memory=memory, brain=brain, skills_brain=skills_brain,
                        tools=tools_registry, souls=cognition.souls,
                        speak_lang=cognition.speak_lang, activity_log=local.activity_log,
                        activity_store=activity_store, affect=local.affect, budget=local.budget,
                        ledgers=ledgers, skills=cognition.skills,
                        threads=threads, proactive=local.proactive,
                        worlds=worlds,
                        step_call=cognition.side.sub, step_duration=step_duration,
                        life_pause=life_pause, circadian=local.circadian)
    bus = EventBus(recorder=outbox.record_delivery)
    _wire_mood_events(bus, local.affect, principals.default())
    # owner 2026-11-03：她主動說的話要寫回自己的線；processing 殭屍任務開機回收
    from agent.brain.compose.boot_wiring import after_threads
    after_threads(proactive=local.proactive, threads=threads, ledgers=ledgers,
                  activity_store=activity_store, entities=entities, outbox=outbox, log=print)
    delivery = ChannelRouter([AppChannel(bus)])
    heartbeat = _wire_heartbeats(
        lives=lives.loops, threads=threads,
        worlds=worlds,
        entities=entities, affect=local.affect, proactive=local.proactive, delivery=delivery,
        activity_log=local.activity_log, budget=local.budget, reminders=reminders,
        timeline=timeline, inner=cognition.inner, memory_search=memory_search,
        principals=principals, souls=cognition.souls, persona_cores=cognition.persona_cores,
        brain=brain, raw_log=cognition.raw_log, speak_lang=cognition.speak_lang,
        tools_registry=tools_registry, base=base, deliveries=deliveries, voice=local.voice)
    heartbeat.attach_sleep(local.circadian)   # owner 2026-11-16：睡着时心跳不叫模型（见 inner/sleep_gate.py）
    backup, distiller = wiring.start_maintenance(
        base, memory=memory, raw_log=cognition.raw_log, entity_ids=entity_ids,
        entity_names=entity_names, vectors=getattr(memory_search, "vectors", None))
    wiring.start_thread_reaper(threads, archive)

    return App(data_dir=base, entities=entities, principals=principals, memory=memory,
               brain=brain, threads=threads, entity_store=entity_store, scheduler=scheduler,
               pipelines=pipelines, router=router, runtime=runtime, archive=archive,
               social=social, activity_store=activity_store, ledgers=ledgers,
               lives=lives.loops,
               step_fn=lives.step_fn, voice=local.voice, tools=tools_registry,
               attention=attention, pending_store=pending_store, guest_store=guest_store,
               stance_store=local.stance_store, stances=local.stances, auth=auth,
               locations=local.locations, distiller=distiller, backup=backup,
               embedder=cognition.search.embedder, vectors=cognition.search.vectors,
               reranker=cognition.search.reranker,
               telemetry=TurnRecorder(base / "logs" / "turns.jsonl", data_root=base),
               bus=bus, outbox=outbox, accounts=accounts, activity_log=local.activity_log,
               proactive=local.proactive, affect=local.affect, delivery=delivery,
               circadian=local.circadian, call_pickup=_call_pickup(cognition.side.elicit),
               budget=local.budget, heartbeat=heartbeat, playground=local.playground,
               userfile=local.userfile, stt=local.stt, speech_hold=build_speech_hold(),
               stickers=stickers, motion=motion_library, media=media_store,
               pending_delivery=deliveries, files=files_store,
               prompt_stats=cognition.prompt_stats,
               persona_cores=cognition.persona_cores, rotation=lives.rotation,
               next_activity=lives.next_activity, worlds=worlds, world_events=world_events)


__all__ = ["App", "build", "build_activity"]
