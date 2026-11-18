"""在場與進出頻道：join／leave／bootstrap、在場輪詢（vc_presence 的決策落地）、人在不在。"""

from __future__ import annotations

import asyncio
import threading
from agent.brain.runtime.voice.vc_presence import (ASK, JOIN, LEAVE, PresenceConfig,
                                                VCPresence)


def publish_voice_world_event(source, *, guild_id: str, is_bot: bool,
                              before_channel: str, after_channel: str) -> None:
    """語音人員變化只投給同 guild 的角色；沒有 guild 就拒絕投遞。"""
    if is_bot or not str(guild_id or "") or before_channel == after_channel:
        return
    app = getattr(getattr(source, "state", None), "app", None)
    events = getattr(app, "world_events", None)
    if events is None:
        return
    if before_channel and after_channel:
        text = "Discord の同じサーバーで通話メンバーが別の部屋に移った"
    elif after_channel:
        text = "Discord の同じサーバーの通話に誰かが入った"
    else:
        text = "Discord の同じサーバーの通話から誰かが出た"
    events.publish("voice_presence", text, guild_id=str(guild_id))


class VoicePresence:
    """在場：進／出頻道、開機補位、在場輪詢、問要不要進來、有沒有人在。"""

    # ── 對外 ────────────────────────────────────────────────────────────────
    async def join(self, client, guild_id: str) -> bool:
        if not self.config.enabled:
            return False
        try:
            import discord
            # 用現役 discordvc 那套接收器（DAVE 只有它處理得了；voice-recv 收到 0 塊）
            from agent.adapter.discord_vc_receiver import VoiceReceiver, load_opus
            load_opus()
        except Exception as exc:                      # noqa: BLE001
            self.last_error = f"receiver_unavailable: {exc}"
            self._log(f"vc: 無法載入接收器（{exc}）")
            return False
        channel = None
        for guild in client.guilds:
            if guild_id and str(guild.id) != str(guild_id):
                continue
            channel = next((item for item in guild.voice_channels
                            if str(item.id) == str(self.config.channel_id)), None)
            if channel is not None:
                break
        if channel is None:
            self.last_error = f"voice_channel_not_found: {self.config.channel_id}"
            self._log(f"vc: 找不到語音頻道 {self.config.channel_id}")
            return False
        try:
            # ⚠️ 用**普通 VoiceClient**（不是 VoiceRecvClient）：接收由我們的 VoiceReceiver 做。
            self.voice_client = await channel.connect(self_deaf=False)
            self.receiver = VoiceReceiver(
                self.voice_client, self._on_block,
                allowed_users=self.allowed_users,
                log=self._log,
                tail_s=self.config.silence_s, tail_max=6)
            # 診斷：直接在 socket 層數封包。分開看「UDP 有沒有進來」與「解碼/路由有沒有壞」
            # ——否則 frames=0 只能猜是防火牆還是函式庫。
            threading.Thread(target=self._packet_watch, daemon=True,
                             name="vc-packets").start()
            self.joined = True
            self._client = client
            self._guild_id = str(guild_id or "")
            try:
                import asyncio
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None
            # 剛進房時對一次名冊（Discord 不會補發「他早就在裡面」的事件）
            members = [member.id for member in getattr(channel, "members", []) or []
                       if not getattr(member, "bot", False)]
            self.presence.sync_members(members)
            self.presence.note_joined()
            self._start_presence_loop()
            self._log(f"vc: 已進語音頻道「{channel.name}」（{channel.id}），開始聽")
            return True
        except Exception as exc:                      # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._log(f"vc: 進頻道失敗 {self.last_error}")
            return False

    # ── 在場決策（進去／退出的執行端）────────────────────────────────────────────
    async def bootstrap(self, client, guild_id: str) -> bool:
        """上線時的第一個判斷：**房裡有人（白名單內）我才進去**，沒人先在外面等。

        owner 2026-10-22 的規矩換掉了「開機就坐進語音房」——沒人的房間她待著只是浪費，
        而且會讓「她聽不到」變成默認狀態。有 `BLUEAGENT_VC_AUTOJOIN=1` 時恢復舊行為。
        """
        self._client = client
        self._guild_id = str(guild_id or "")
        try:
            import asyncio
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        channel = None
        for guild in client.guilds:
            if guild_id and str(guild.id) != str(guild_id):
                continue
            channel = next((item for item in guild.voice_channels
                            if str(item.id) == str(self.config.channel_id)), None)
            if channel is not None:
                break
        humans = [member.id for member in getattr(channel, "members", []) or []
                  if not getattr(member, "bot", False)] if channel is not None else []
        self.presence.sync_members(humans)
        self._start_presence_loop()
        wanted = [str(item) for item in humans
                  if self.presence._is_allowed(str(item))]      # noqa: SLF001 - 同一個模組內的判斷
        if wanted or self.presence.config.autojoin:
            return await self.join(client, guild_id)
        self._log("vc: 房裡沒人 ⇒ 她先不進去（有人進來會問要不要進）")
        return False

    async def leave(self, reason: str = "empty_room") -> bool:
        """她自己退房（不是關掉監聽開關）。"""
        client = self.voice_client
        try:
            if self.receiver is not None:
                self.receiver.stop()
        except Exception:                                  # noqa: BLE001
            pass
        self.receiver = None
        self.joined = False
        was = client is not None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:                        # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._log(f"vc: 退房失敗 {self.last_error}")
                return False
        self.voice_client = None
        self.presence.note_left()
        self._note_activity(f"我从语音房退出来了（{reason}）")
        self._log(f"vc: 她退出了語音頻道（{reason}）")
        return was

    def request_join(self, reason: str = "asked") -> bool:
        """任何執行緒都能叫：請她進房（真的連線在 event loop 上做）。"""
        client, loop = self._client, self._loop
        if client is None or loop is None:
            self._log("vc: 進房請求失敗（Discord 還沒連上）")
            return False
        try:
            import asyncio
            asyncio.run_coroutine_threadsafe(self.join(client, self._guild_id), loop)
            self._log(f"vc: 她決定進語音房（{reason}）")
            return True
        except Exception as exc:                            # noqa: BLE001
            self._log(f"vc: 進房請求失敗 {type(exc).__name__}: {exc}")
            return False

    def request_leave(self, reason: str = "asked") -> bool:
        client, loop = self.voice_client, self._loop
        if client is None or loop is None:
            return False
        try:
            import asyncio
            asyncio.run_coroutine_threadsafe(self.leave(reason), loop)
            return True
        except Exception as exc:                            # noqa: BLE001
            self._log(f"vc: 退房請求失敗 {type(exc).__name__}: {exc}")
            return False

    def observe_voice_state(self, *, user_id, is_bot: bool, before_channel: str,
                            after_channel: str, quiet: bool = False) -> list[dict]:
        """Discord 的 `on_voice_state_update` 轉進來（adapter 只搬事實，判斷在政策層）。"""
        actions = self.presence.observe(user_id=user_id, is_bot=is_bot,
                                        before_channel=before_channel,
                                        after_channel=after_channel, quiet=quiet)
        for action in actions:
            self._run_action(action)
        return actions

    def _start_presence_loop(self) -> None:
        if self._presence_thread is not None or self.presence.config.empty_leave_s <= 0:
            return
        self._presence_thread = threading.Thread(target=self._presence_loop,
                                                 daemon=True, name="vc-presence")
        self._presence_thread.start()

    def _presence_loop(self) -> None:
        """每 2.5 秒問一次政策層：現在該不該自己退。"""
        while not self._presence_stop.wait(2.5):
            try:
                for action in self.presence.tick():
                    self._run_action(action)
            except Exception as exc:                        # noqa: BLE001
                self._log(f"vc: 在場決策失敗 {type(exc).__name__}: {exc}")

    def _run_action(self, action: dict) -> None:
        kind = str(action.get("action") or "")
        if kind == LEAVE:
            self.request_leave(str(action.get("reason") or "empty_room"))
        elif kind == JOIN:
            self.request_join(str(action.get("reason") or "human_arrived"))
        elif kind == ASK:
            self._ask_join(str(action.get("user_id") or ""))

    def _ask_join(self, user_id: str) -> None:
        """有人進房、她不在 ⇒ 問一句（不擅自進去）。"""
        text = "入ろうか？（入ってほしいなら「入って」って言ってね）"
        posted = False
        sender = getattr(self.source, "send_channel_text", None)
        if sender is not None:
            try:
                posted = bool(sender(self.config.channel_id, text))
            except Exception:                               # noqa: BLE001
                posted = False
        self._log(f"vc: 問要不要進房（user={user_id or '-'}，貼文字={'ok' if posted else 'failed'}）")
        self._note_activity("有人进了语音房，我问他要不要我进去")

    def _note_activity(self, text: str) -> None:
        """記進她自己的私記（不推訊息、不向主人匯報）。"""
        app = getattr(getattr(self.source, "state", None), "app", None)
        log = getattr(app, "activity_log", None)
        if log is None:
            return
        entity = str(getattr(getattr(self.source, "config", None), "default_entity", "") or "mira")
        try:
            log.append(entity, kind="voice", text=text, visibility="private")
        except Exception:                                   # noqa: BLE001
            pass

    # ── 主動發言用（見 runtime/delivery/delivery.py 的 DiscordVoiceChannel）─────────────
    def humans_present(self) -> bool:
        """頻道裡現在有沒有人在（除了 bot）。她在裡面說話才有意義。"""
        try:
            client = self.voice_client
            if client is None or not client.is_connected():
                return False
            channel = getattr(client, "channel", None)
            members = list(getattr(channel, "members", []) or [])
            bots = {member.id for member in members if getattr(member, "bot", False)}
            return any(member.id not in bots for member in members)
        except Exception:                                 # noqa: BLE001
            return False
