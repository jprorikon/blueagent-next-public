"""啟動入口：參數、環境檔、建應用、起 HTTP、接外部整合、收尾。

各整合（Discord／TRPG／告警／信道）的細節在 `server/integrations/`；這裡只排順序。
"""

from __future__ import annotations

import argparse
import os
import pathlib
from http.server import ThreadingHTTPServer

from agent.adapter.tts_bridge import tts_label
from agent.brain.factory import brain_label
from agent.brain.compose.bootstrap import build
from agent.brain.domain.attention import AttentionConfig
from agent.brain.compose.state import AppState
from agent.server import http


def load_env_file(path: str) -> int:
    """讀 `KEY=VALUE`（`#` 註解、可有引號）；**已存在的環境變數不覆蓋**。

    為什麼要這個：現役桥的金鑰放在 `.env` 裡（600）。新倉不想把金鑰複製一份進 repo，
    也不想在命令行上帶（會進 shell history／審批日誌）——指一個檔案路徑最乾淨。
    """
    loaded = 0
    try:
        lines = pathlib.Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BlueAgent Next server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--brain-delay", type=float, default=0.35)
    parser.add_argument("--shadow", action="store_true",
                        help="注意力网关影子模式：照常记录，不改变行为")
    return parser.parse_args(argv)


def load_env_files() -> None:
    """`BLUEAGENT_ENV_FILE`（逗號／冒號分隔）裡的檔依序載入；已存在的變數不覆蓋。"""
    env_files = [item for item in str(os.environ.get("BLUEAGENT_ENV_FILE") or "")
                 .replace(",", ":").split(":") if item.strip()]
    for env_file in env_files:
        loaded = load_env_file(env_file.strip())
        print(f"  env:    {env_file.strip()}（載入 {loaded} 項）", flush=True)


def build_state(args: argparse.Namespace) -> AppState:
    """應用狀態＋HTTP 層要的兩個小倉（媒體、五子棋）。"""
    state = AppState(build(brain_delay=args.brain_delay,
                           attention_config=AttentionConfig(shadow=args.shadow)))
    # 媒體（圖片）存放：`data/media/`（P4 看圖／相册）；沒有這個目錄也能跑，只是圖片那條回 503
    try:
        from agent.brain.media.store import MediaStore
        # 優先沿用 bootstrap 建好的那一個（委任出圖也會寫進去，兩邊必須是同一個實例）
        state.media = getattr(state.app, "media", None) or MediaStore(
            pathlib.Path(os.environ.get("BLUEAGENT_DATA_DIR", "data")) / "media")
    except Exception as exc:                       # noqa: BLE001
        print(f"  media:  off（{type(exc).__name__}: {exc}）", flush=True)
    # 五子棋（P6）：一局用完即棄、與會話無關
    from agent.brain.games.session import GameStore
    state.games = GameStore()
    return state


def print_banner(state: AppState, args: argparse.Namespace) -> None:
    print(f"BlueAgent Next · http://{args.host}:{args.port}/", flush=True)
    print("  server: brain runtime · client: client/", flush=True)
    print(f"  brain:  {brain_label(state.app.brain)}", flush=True)
    print(f"  voice:  {tts_label(state.app.voice.tts)}", flush=True)
    stt = getattr(state.app, "stt", None)
    print(f"  stt:    {stt.engine if stt is not None else 'off'}"
          f"{'（名字修正 on）' if stt is not None and stt.config.fix_names else ''}", flush=True)


def start_integrations(state: AppState) -> None:
    """Discord 連上了才接 TRPG 席位、告警、Discord 信道（沒開就一個都不載入）。"""
    from agent.server.integrations.discord import start_discord
    source = start_discord(state)
    if source is None:
        print("  discord: off（沒有 DISCORD_BOT_TOKEN）", flush=True)
        return
    from agent.server.integrations.alerts import start_alerts
    from agent.server.integrations.delivery import register_discord_channels
    from agent.server.integrations.trpg import start_trpg
    start_trpg(state, source)
    start_alerts(state, source)
    # 多顆 bot（owner 2026-10-27「做 1 到 4」）：主動訊息／語音按角色挑自己的 bot
    register_discord_channels(state, source, getattr(state.app, "discord_by_entity", None))
    print(f"  discord: 連線中（guild={source.config.guild_id} "
          f"channel={source.config.channel_id} owners={sorted(source.config.owner_ids)}）", flush=True)
    table = getattr(state.app, "discord_by_entity", None) or {}
    if len(table) > 1:
        print(f"  discord: {len(table)} 顆 bot 在線（{', '.join(sorted(table))}）", flush=True)


def print_owner_token(state: AppState, args: argparse.Namespace) -> None:
    """⚠️ 只印指紋。明文 token 只在「這次剛建立／剛輪替」時才有值，且會提醒一次。"""
    auth = state.app.auth
    print(f"  owner token: {auth.owner_token_fingerprint}"
          f"（存在 data/auth.json，600）", flush=True)
    if auth.created_now and auth.owner_token:
        print("  ⚠️ 新的 owner token（只顯示這次，請抄走）："
              f"{auth.owner_token}", flush=True)
    print(f"  owner URL:   http://{args.host}:{args.port}/?token="
          "（token 用 ?token=<你的 owner token> 帶上）", flush=True)


def start_heartbeat(state: AppState):
    """心跳：她自己的活性（情緒推進 + 想說話就說）；沒開就是純被動。"""
    beat = getattr(state.app, "heartbeat", None)
    if beat is None:
        return None
    beat.start()
    for name, one in getattr(beat, "beats", {}).items():
        print(f"  heartbeat[{name}]: 每 {one.interval_s:.0f}s 一跳"
              f"（門檻 {one.threshold:.2f}）", flush=True)
    return beat


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    load_env_files()
    state = build_state(args)
    http.set_state(state)
    httpd = ThreadingHTTPServer((args.host, args.port), http.Handler)
    httpd.daemon_threads = True
    print_banner(state, args)
    start_integrations(state)
    print_owner_token(state, args)
    beat = start_heartbeat(state)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", flush=True)
    finally:
        httpd.server_close()
        if beat is not None:
            beat.stop()
        if getattr(state.app, "discord", None) is not None:
            state.app.discord.stop()
