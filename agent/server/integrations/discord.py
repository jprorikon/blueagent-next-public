"""Discord 源（文字＋可選的語音頻道）：有 token 才連。

從 `server/main.main()` 拆出來（2026-10-25）。

2026-10-27（owner「做 1 到 4」）：改成**可同時跑多顆 bot**——一顆 bot ↔ 一個實體。
主 bot（mira）用無前綴那組環境；其他角色用 `BLUEAGENT_DISCORD_<ENTITY>_*`（見
`adapter/discord_source/bots.py`）。回傳值仍是主源（相容），
另外把每顆源的對照表掛在 `state.app.discord_by_entity`。
"""

from __future__ import annotations

import os


def _log(message: str) -> None:
    print(f"  {message}", flush=True)


def make_barge_in_handler(state, entity: str):
    """打斷（barge-in）：主人在同一個 VC 開口 ⇒ 停播 + runtime 收嘴 + 記一筆。"""

    def interrupted(user_id: str, epoch: int) -> None:
        record = state.app.voice.interrupt(entity, reason="barge_in", principal=str(user_id))
        # 被打斷也要算進心情（owner 2026-10-24：量表要能往下）
        affect = getattr(state.app, "affect", None)
        if affect is not None:
            try:
                affect.interrupted(entity, source_event_id=f"barge:{entity}:{epoch}")
            except Exception as exc:                # noqa: BLE001 - 記帳不能擋打斷
                print(f"  vc: 打斷記帳失敗（{type(exc).__name__}: {exc}）", flush=True)
        recorder = getattr(state.app, "telemetry", None)
        if recorder is not None:
            recorder.record(event="barge_in", entity=entity,
                            principal=str(user_id), epoch=epoch,
                            floor_holder=record.get("floor_holder") or "")
        print(f"  vc: 打斷（user={user_id or '-'}, entity={entity}, epoch={epoch}）", flush=True)

    return interrupted


def _attach_voice(state, source, env=None) -> None:
    """語音頻道（要 `BLUEAGENT_DISCORD_VC=1`）：進來的話走同一條管道，回話聲音播回頻道。

    `env` 必須是**這顆 bot 自己的**那份（多顆 bot 時各自的 VC id／開關不同）。
    """
    from agent.adapter.discord_voice import build_voice
    values = env if env is not None else os.environ
    session = build_voice(values, source, log=_log)
    if session is None:
        return
    source.voice = session
    print(f"  vc[{getattr(source.config, 'default_entity', '?')}]: "
          f"已設定語音頻道 {session.config.channel_id}（靜音 {session.config.silence_s}s 斷句）",
          flush=True)
    entity = str(getattr(source.config, "default_entity", "") or "mira")
    session.on_interrupt = make_barge_in_handler(state, entity)
    print(f"  vc[{entity}]: 打斷{'開' if session.config.barge_in else '關'}", flush=True)


def _start_one(state, env, label: str, *, voice: bool = True):
    """建一顆源、（可選）掛語音、連線。連上了回源，否則 None。"""
    from agent.adapter.discord_source import build_discord
    source = build_discord(state, env=env, log=_log)
    if source is None:
        return None
    if voice:
        _attach_voice(state, source, env)
    if not source.start():
        _log(f"discord[{label}]: 連線失敗")
        return None
    _log(f"discord[{label}]: 已連上（entity={source.config.default_entity} "
         f"channel={source.config.channel_id}）")
    return source


def start_discord(state):
    """主 bot ＋ 每個 `BLUEAGENT_DISCORD_<ENTITY>_TOKEN` 各一顆；回主源（相容舊呼叫端）。

    掛在 `state.app`：
      · `discord`：主源（mira／或第一顆成功的）——舊程式碼照用；
      · `discord_by_entity`：`{entity: source}`——主動訊息／語音按角色挑正確的 bot。
    """
    from agent.adapter.discord_source.bots import bots_from_env, sources_by_entity

    primary = _start_one(state, os.environ, "primary")
    sources = []
    for entity, env_map in sorted(bots_from_env(os.environ).items()):
        source = _start_one(state, env_map, entity)
        if source is not None:
            sources.append(source)
    table = sources_by_entity(([primary] if primary is not None else []) + sources)
    state.app.discord_by_entity = table
    main = primary or (sources[0] if sources else None)
    if main is None:
        return None
    state.app.discord = main
    if len(table) > 1:
        _log(f"discord: 共 {len(table)} 顆 bot 在線（{', '.join(sorted(table))}）")
    return main


__all__ = ["make_barge_in_handler", "start_discord"]
