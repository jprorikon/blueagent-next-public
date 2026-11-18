"""TRPG 席位：桌端 kick 叫她 → 她看牌面 → 說話＋宣告一個合法動作。

從 `server/main.main()` 拆出來（2026-10-25）；七個閉包改成有名字的工廠。
"""

from __future__ import annotations

import os


def _log(message: str) -> None:
    print(f"  {message}", flush=True)


#: 遊戲事件 → AEM：**target 是 game/table，不是 owner**
#: （owner 2026-10-22：遊戲情緒要與對真人的情緒分開，不能記到主人頭上）
GAME_AEM = {
    "damage": "concern", "death": "concern",
    "stellaroid_killed": "pleasure", "monster_damaged": "pleasure",
    "exp_gained": "pleasure", "level_up": "pleasure", "star_up": "pleasure",
    "search_loot": "interest", "entrance_found": "interest",
    "memory_change": "interest",
    "turn_skipped": "frustration", "ended": "uncertainty",
}


def make_speaker(state, config):
    """她在桌上說一句：走她自己的回合（指名叫她，source 標成 `trpg:<table>`）。"""

    def speak(prompt: str, purpose: str = "trpg") -> str:
        # ⚠ 不要覆寫 source：`trpg:table-1` 才會被判成 Purpose.TRPG ⇒ 管線 `trpg.turn`；
        #   寫成裸 `trpg` 會被當 AMBIENT ⇒ `ambient.note`（沒註冊的管線，實測 KeyError）。
        # ⚠ 不再用 ownerless：系統裡每個 entity 各自持有行為，`ownerless` 無法判定
        # 「該由哪個 entity 執行」（owner 2026-10-22）。TRPG 就是**指名叫她**：
        # 發起人＝master、owner＝她、執行者＝她，只是 source 標成 trpg:<table>。
        result = state.send(prompt, entity=config.entity, principal="owner",
                            force_reply=True, mode="chat",
                            source_override=f"trpg:{config.table_id or 'table'}")
        text = state.reply_text(result)
        if not text:
            text = _text_from_frames(result.get("frames") or [])
        # 卡點取證：她「沒說話」時要看得出是哪一段空掉的（決策＝沒回？腦空？錯誤？）
        decision = result.get("decision") or {}
        print(f"  trpg: speak → {len(text)} 字 / ok={result.get('ok')} "
              f"/ error={str(result.get('error') or '')[:80]} "
              f"/ pipeline={decision.get('pipeline')} "
              f"/ engage={decision.get('engage')} "
              f"/ frames={len(result.get('frames') or [])} "
              f"/ yielded={result.get('yielded')} "
              f"/ skip={str(result.get('skipped') or result.get('reason') or '')[:40]}",
              flush=True)
        return text

    return speak


def _text_from_frames(frames: list) -> str:
    """`done` 幀沒有全文時，把 delta／part 的文字接起來（frames 明明有東西卻取不到字）。"""
    pieces = []
    for frame in frames:
        payload = frame.get("payload") if isinstance(frame, dict) else None
        if not isinstance(payload, dict):
            continue
        body = str(payload.get("text") or "")
        if body and frame.get("kind") in ("delta", "part", "say", "speak"):
            pieces.append(body)
    text = "".join(pieces).strip() if pieces else ""
    if pieces:
        print(f"  trpg: 由 frames 還原台詞 {len(text)} 字", flush=True)
    kinds: dict = {}
    for frame in frames:
        kinds[str((frame or {}).get("kind"))] = kinds.get(str((frame or {}).get("kind")), 0) + 1
    print(f"  trpg: done 幀無全文（frames 種類 {kinds}）", flush=True)
    return text


def make_reporter(state, config, source, target: str):
    """桌上的匯報：本地檔＋她的私記；`target`＝local（預設）／dm／discord。

    owner 2026-10-22：「trpg 还是不要往 discord 里面投好一点」⇒ 預設只落本地。
    """

    def report(text: str, meta: dict) -> bool:
        body = str(text).strip()
        if not body:
            return False
        from agent.brain.runtime.trpg_reports import append_report
        if getattr(state.app, "data_dir", None) is not None:
            append_report(state.app.data_dir, entity=config.entity,
                          text=body, table=str((meta or {}).get("table") or ""),
                          seat=str((meta or {}).get("seat") or ""),
                          round_no=int((meta or {}).get("round") or 0))
        # 她的私記（只有她看得到）——桌上發生的事要能被下次對話帶到
        log = getattr(state.app, "activity_log", None)
        if log is not None and hasattr(log, "append"):
            try:
                log.append(config.entity, kind="trpg-report", text=body,
                           visibility="private", activity="trpg",
                           step=int((meta or {}).get("round") or 0))
            except Exception:                # noqa: BLE001
                pass
        if target == "dm":
            try:
                return bool(source.send_dm("owner", body))
            except Exception:                # noqa: BLE001
                return False
        if target == "discord":
            try:
                return bool(source.send_text(body))
            except Exception:                # noqa: BLE001
                return False
        return True                          # local：寫檔就算成功

    return report


def make_intensity(state, config):
    """她當下的情緒強度（0–1）：|valence| 與 arousal 取大。
    情緒是「要不要主動講」的閘——平盤就安靜打牌（owner 2026-10-22）。"""

    def intensity() -> float:
        affect = getattr(state.app, "affect", None)
        if affect is None:
            return 0.0
        view = affect.snapshot(config.entity) or {}
        body = view.get("affect") if isinstance(view, dict) else {}
        body = body or {}
        try:
            valence = abs(float(body.get("valence") or 0.0))
            arousal = float(body.get("arousal") or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return max(valence, arousal)

    return intensity


def make_game_affect(state, config):
    """遊戲事件記進情緒（記在桌上，不記在主人頭上）。

    2026-11-09 檢查：以前走 `appraise(target_type="game")`——核心不收 `game` 這個 target、
    `appraise` 又要一條真的 message ⇒ **每一筆都 INVALID_INPUT**，桌上發生什麼都沒進過心情。
    改走 `outcome`（她自己這邊的事；不掛 message、不動主人缺席計時），target＝task:`trpg-<桌>`。
    乘區與磨損由席位那邊算（`adapter/trpg/mood.py`），這裡不再折半（`dampen=False`）。
    """

    def game_affect(kind: str, delta_v: float, delta_a: float, payload: dict) -> None:
        affect_obj = getattr(state.app, "affect", None)
        if affect_obj is None:
            return
        appraisal = GAME_AEM.get(str(kind))
        if appraisal is None:
            return
        try:
            result = affect_obj.outcome(
                config.entity, kind=appraisal,
                delta_v=float(delta_v), delta_a=float(delta_a),
                event_id=f"trpg:{config.table_id}:{getattr(config, 'session', '')}:{(payload or {}).get('seq')}",
                target_type="task", target_id=f"trpg-{config.table_id or 'table'}",
                dampen=False)
            if isinstance(result, dict) and not result.get("ok") and not result.get("duplicate"):
                print(f"  trpg: AEM 記帳被拒（{result.get('error')}）", flush=True)
        except Exception as exc:            # noqa: BLE001 - 情緒壞了不能擋玩
            print(f"  trpg: AEM 記帳失敗（{type(exc).__name__}: {exc}）", flush=True)

    return game_affect


def make_table_closed_notifier(state, config):
    """她離席 ⇒ 通知 App 把桌畫面收掉（回正常聊天頁）。"""

    def notify(reason: str) -> None:
        router = getattr(state.app, "delivery", None)
        frame = {"kind": "trpg_closed", "reason": str(reason or ""),
                 "entity": str(config.entity or "mira"),
                 "principal": "owner", "visibility": "private"}
        try:
            if router is not None:
                print(f"  trpg: 離席通知 → {router.send('owner', frame)}", flush=True)
        except Exception as exc:            # noqa: BLE001
            print(f"  trpg: 離席通知失敗（{type(exc).__name__}: {exc}）", flush=True)

    return notify


def make_master_notice(state, config):
    """她「不想玩」這類話要明說給主人：走主動訊息那條（App → Discord DM，永不貼群）。"""

    def notice(text: str) -> None:
        body = str(text or "").strip()
        if not body:
            return
        router = getattr(state.app, "delivery", None)
        frame = {"kind": "proactive", "text": body,
                 "entity": str(config.entity or "mira"),
                 "principal": "owner", "visibility": "private",
                 "reason": "trpg_decline"}
        try:
            if router is not None:
                print(f"  trpg: 告知主人「不想玩」→ {router.send('owner', frame)}", flush=True)
            else:
                print("  trpg: 告知主人（沒有 delivery，只記 log）", flush=True)
        except Exception as exc:            # noqa: BLE001
            print(f"  trpg: 告知失敗（{type(exc).__name__}: {exc}）", flush=True)

    return notice


def make_phase_notifier(state, cfg):
    """桌進了下一輪／結束了 ⇒ 投一則世界事件給**這一席的角色**。

    讀的是**這一席自己的** `cfg`（kick 換桌時只改它）。2026-11-11：一桌多席（c16b418）之後
    這支還讀外層那份 config ⇒ 桌號停在環境變數那一個（通常是空的）、而且只投給預設角色。
    """

    def phase(kind: str, round_no: int) -> None:
        events = getattr(state.app, "world_events", None)
        if events is None or not cfg.table_id:
            return
        text = "TRPG のこの局が終わった" if kind == "ended" \
            else f"TRPG が第 {round_no} ラウンドに進んだ"
        events.publish("trpg_phase", text, entities=(cfg.entity,), principals=("owner",),
                       table_id=cfg.table_id)

    return phase


def pick_seat(app, body) -> tuple[object, str]:
    """`/trpg/kick` 交給哪一席：回 `(seat, unknown_seat)`；`unknown_seat` 非空時呼叫端（驗完 token）回 404。

    一桌多席（owner 2026-11-09）：kick 帶著 seat ⇒ 交給那一席；沒帶就交預設席。
    同桌多席時，**沒登記的席位不交給預設那一席**（2026-11-11）：`TrpgSeat.adopt` 會跟著 kick
    把自己換成那個席位，預設角色從此坐到別人的位子上。只有一席（舊的單席設定）照舊跟著 kick 換席。
    """
    seats = getattr(app, "trpg_seats", None) or {}
    wanted = str((body or {}).get("seat") or "").strip()
    seat = seats.get(wanted) if wanted else None
    unknown = wanted if (seat is None and wanted and len(seats) > 1) else ""
    # 未知席位也回預設席（只拿它的 kick token 驗身分）；呼叫端驗過 token 才回 404，不讓沒 token 的人探席位
    return (seat if seat is not None else getattr(app, "trpg", None)), unknown


def seat_specs(values, *, default_seat: str = "", default_entity: str = "") -> list[tuple[str, str]]:
    """這一桌要坐哪幾席：`BLUEAGENT_TRPG_SEATS="p1:mira,p2:aster,p3:nori"`。

    沒設就退回單席（`BLUEAGENT_TRPG_SEAT`＋`BLUEAGENT_TRPG_ENTITY`，舊行為）。
    為什麼要一次坐多席（owner 2026-11-09「开一个桌子，让她们三个实际玩一局」）：
    一顆 agent 行程本來只認一個實體，三個角色同桌就得三個席位、各自一個 TrpgSeat
    （各自看自己的投影、各自出手、各自記心情）。
    """
    raw = str((values or {}).get("BLUEAGENT_TRPG_SEATS") or "").strip()
    specs: list[tuple[str, str]] = []
    for part in raw.split(","):
        seat, _, entity = part.partition(":")
        seat, entity = seat.strip(), entity.strip()
        if seat and entity:
            specs.append((seat, entity))
    if specs:
        return specs
    if str(default_seat or "").strip() and str(default_entity or "").strip():
        return [(str(default_seat).strip(), str(default_entity).strip())]
    return []


def start_trpg(state, source) -> None:
    """有設定桌與席位才開；桌不在也不該擋開機（失敗只印一行）。"""
    try:
        from agent.adapter.trpg import TrpgConfig, TrpgMcp, TrpgSeat
        config = TrpgConfig.from_env(os.environ)
        if not config.ready:
            if config.enabled:
                print("  trpg:    已開但缺 SEAT／KICK_TOKEN ⇒ 不啟用", flush=True)
            return
        # `BLUEAGENT_TRPG_REPORT_TARGET`：local（預設）／dm／discord
        target = str(os.environ.get("BLUEAGENT_TRPG_REPORT_TARGET") or "local").lower()
        report_on = str(os.environ.get("BLUEAGENT_TRPG_REPORT") or "1").strip().lower() \
            not in {"0", "off", "false", "no"}
        def build_seat(seat_id: str, entity: str):
            """一席＝一個實體：自己的 config／MCP／發話人／匯報／心情。"""
            cfg = TrpgConfig.from_env(dict(os.environ, BLUEAGENT_TRPG_SEAT=seat_id,
                                           BLUEAGENT_TRPG_ENTITY=entity))
            cfg.table_id, cfg.session = config.table_id, config.session
            return TrpgSeat(cfg, TrpgMcp(cfg), make_speaker(state, cfg),
                            log=_log,
                            report=make_reporter(state, cfg, source, target) if report_on else None,
                            report_every_rounds=int(
                                os.environ.get("BLUEAGENT_TRPG_REPORT_EVERY_ROUNDS") or 1),
                            report_min_gap_s=float(os.environ.get("BLUEAGENT_TRPG_REPORT_GAP_S") or 90),
                            intensity=make_intensity(state, cfg),
                            min_intensity=float(os.environ.get(
                                "BLUEAGENT_TRPG_REPORT_MIN_INTENSITY") or 0.12),
                            affect=make_game_affect(state, cfg),
                            refuse_below=float(os.environ.get("BLUEAGENT_TRPG_REFUSE_V") or -0.08),
                            mood=lambda e=cfg.entity: (getattr(state.app, "affect", None).mood(e)
                                                       if getattr(state.app, "affect", None) else {}),
                            notice=make_master_notice(state, cfg),
                            on_leave=make_table_closed_notifier(state, cfg),
                            on_phase=make_phase_notifier(state, cfg))

        registry: dict[str, object] = {}
        for seat_id, entity in seat_specs(os.environ, default_seat=config.seat,
                                          default_entity=config.entity):
            registry[seat_id] = build_seat(seat_id, entity)
        seat = registry.get(config.seat) or next(iter(registry.values()))
        #: 一桌多席：`/trpg/kick` 按 kick 帶來的 seat 分派（一席一把鎖，互不阻塞）。
        state.app.trpg_seats = registry
        state.app.trpg = seat
        for item in registry.values():                 # 每個角色的世界看「她那一席」現在坐哪一桌
            world = (getattr(state.app, "worlds", None) or {}).get(item.config.entity)
            if world is not None:
                world.table_ids = (lambda cfg=item.config:
                                   ((cfg.table_id,) if cfg.table_id else ()))
        if len(registry) > 1:
            print("  trpg:    同桌席位 " + "、".join(
                f"{sid}→{getattr(item.config, 'entity', '?')}" for sid, item in registry.items()),
                flush=True)
        report_label = (f"每 {seat.report_every_rounds} 輪・情緒≥{seat.min_intensity:.2f}"
                        if report_on else "off")
        print(f"  trpg:    席位 {config.seat} @ {config.table_id}"
              f"（kick: POST /trpg/kick，"
              f"token {'on' if config.kick_token else 'off'}；匯報 {report_label}）",
              flush=True)
    except Exception as exc:                          # noqa: BLE001 - 桌不在也不該擋開機
        print(f"  trpg: 啟動失敗（{type(exc).__name__}: {exc}）", flush=True)


__all__ = ["GAME_AEM", "make_game_affect", "make_intensity", "make_master_notice",
           "make_phase_notifier", "make_reporter", "pick_seat", "make_speaker", "make_table_closed_notifier",
           "seat_specs", "start_trpg"]
