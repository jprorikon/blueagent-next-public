"""傳輸與連線生命週期：discord.py client、送出（頻道／私聊）、後續交付、啟動與停止。"""

from __future__ import annotations

import asyncio
import io
import threading
import time
from agent.brain.domain.social_condition import NORMATIVE, classify, has_substance


class DiscordTransport:
    """傳輸與連線：送文字／私聊、後續交付、建 client、起停與事件迴圈。"""

    def _addressee_for(self, message, client, *, channel_id: str, author_id: str,
                       mentioned: bool, reply_to_me: bool, reply_other_id: str):
        """這則訊息在對誰說（`runtime/addressee.py`）；回 `(收件人, 理由)`。

        喚醒詞表裡有三個角色的名字（ミラ／アスター／ノリ），但這顆 bot 只代表**一個實體**
        （`entity_for()` 永遠回 default_entity）⇒ 叫別的角色不算叫她。
        """
        mentioned_other = [str(getattr(user, "id", "")) for user in (message.mentions or [])
                           if client.user is None
                           or str(getattr(user, "id", "")) != str(client.user.id)]
        names_map = dict(getattr(self.config, "names", {}) or {})
        default_entity = str(getattr(self.config, "default_entity", "") or "")
        my_names = tuple(name for name, entity in names_map.items()
                         if not default_entity or str(entity) == default_entity)
        other_names = tuple(name for name, entity in names_map.items()
                            if default_entity and str(entity) != default_entity)
        return self.addressee.resolve(
            channel_id=channel_id, author_id=author_id, text=message.content or "",
            mentioned_me=mentioned, mentioned_other_ids=mentioned_other,
            reply_to_me=reply_to_me, reply_other_id=reply_other_id,
            my_names=my_names, other_names=other_names, roster=dict(self._roster))

    # ── 主動發言的出口（給 `runtime/delivery/delivery.py` 的 DiscordChannel 用）──────────
    def can_dm(self, principal: str) -> bool:
        """知道他的 Discord id 才談得上 DM（owner 就是 owner_ids 裡那個）。"""
        return bool(self._discord_id_for(principal))

    def send_dm(self, principal: str, text: str) -> bool:
        user_id = self._discord_id_for(principal)
        if not user_id or not text:
            return False
        return self._send_async(int(user_id), text, dm=True)

    def send_text(self, text: str) -> bool:
        channel_id = str(getattr(self.config, "channel_id", "") or "")
        if not channel_id or not text:
            return False
        return self._send_async(int(channel_id), text, dm=False)

    def _discord_id_for(self, principal: str) -> str:
        if str(principal) == "owner":
            ids = list(getattr(self.config, "owner_ids", ()) or ())
            return str(ids[0]) if ids else ""
        found = getattr(self, "_principal_ids", {}) or {}
        return str(found.get(str(principal)) or "")

    def _send_async(self, target_id: int, text: str, *, dm: bool) -> bool:
        client, loop = self._client, getattr(self, "_loop", None)
        if client is None or loop is None:
            return False
        try:
            import asyncio
            if dm:
                async def _dm() -> None:
                    user = client.get_user(target_id) or await client.fetch_user(target_id)
                    await user.send(text)
                asyncio.run_coroutine_threadsafe(_dm(), loop)
            else:
                async def _post() -> None:
                    channel = client.get_channel(target_id)
                    if channel is not None:
                        try:
                            await channel.send(text)
                        except Exception as exc:          # noqa: BLE001 - 丟在 coroutine 裡會靜默
                            self._log(f"discord: 貼文字失敗（{type(exc).__name__}: {exc}）")
                    else:
                        self._log(f"discord: 找不到頻道 {target_id}（貼文字失敗）")
                asyncio.run_coroutine_threadsafe(_post(), loop)
            return True
        except Exception as exc:                          # noqa: BLE001
            self._log(f"discord: 送出失敗（{type(exc).__name__}: {exc}）")
            return False

    def _post_followup(self, channel_id: str, result: dict, *, speak: bool = False) -> None:
        """背景任務做完 ⇒ 把結果貼回頻道（第二條訊息）。

        語音那條路本來就是「先出聲、後補完整答案」，所以補的那條一定要送到，
        否則主人只聽到一句「好，我去看看」然後永遠沒有下文。

        `speak=True`（語音頻道）：**完整答案也要唸出來**——只貼文字會讓人覺得
        「有一段話沒被讀出來」（2026-10-22 owner 的實測回報）。
        """
        text = (self.state.reply_text(result)
                if hasattr(self.state, "reply_text")
                else str(result.get("reply") or "")).strip()
        if not text or not channel_id:
            return
        client, loop = self._client, getattr(self, "_loop", None)
        if client is None or loop is None:
            return
        audio = self.speak_followup(result) if speak else b""

        async def _send() -> None:
            channel = client.get_channel(int(channel_id))
            if channel is None:
                return
            if audio:
                import discord
                await channel.send(text, file=discord.File(io.BytesIO(audio),
                                                           filename="reply.wav"))
            else:
                await channel.send(text)

        try:
            import asyncio
            asyncio.run_coroutine_threadsafe(_send(), loop)
        except Exception as exc:                          # noqa: BLE001
            self._log(f"discord: 後續交付失敗（{type(exc).__name__}: {exc}）")

    def send_channel_text(self, channel_id: str, text: str) -> bool:
        """把一句話貼進指定頻道（語音頻道也有文字聊天；她在那裡說話時順便貼字）。"""
        if not channel_id or not str(text or "").strip():
            return False
        try:
            return self._send_async(int(channel_id), str(text).strip(), dm=False)
        except (TypeError, ValueError):
            return False

    # ── discord.py 薄殼 ─────────────────────────────────────────────────────
    def _make_client(self, discord, *, message_content: bool):
        """建 client 並掛事件（只做適配，判斷全在上面的 `handle`）。"""
        intents = discord.Intents.default()
        intents.message_content = bool(message_content)
        intents.guilds = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():                             # noqa: ANN202
            self._remember_loop()
            self._log(f"discord: 已上線 {client.user}（guild={self.config.guild_id} "
                      f"channel={self.config.channel_id} message_content={message_content}）")
            if self.voice is not None:
                # 在場決策：房裡有人（白名單內）才進去；沒人就先在外面等，有人進來會問一句
                await self.voice.bootstrap(client, self.config.guild_id)

        @client.event
        async def on_voice_state_update(member, before, after):   # noqa: ANN001, ANN202
            """誰進出語音頻道：沒有這行日志，「她聽不到」會變成猜謎。"""
            if before.channel == after.channel:
                return
            went = getattr(after.channel, "name", None) or "（離開）"
            came = getattr(before.channel, "name", None) or "（外面）"
            self._log(f"voice-state: {getattr(member, 'display_name', member)} "
                      f"{came} → {went}（ch={getattr(after.channel, 'id', '')}）")
            from agent.adapter.discord_voice.presence import publish_voice_world_event
            publish_voice_world_event(self,
                guild_id=str(getattr(getattr(member, "guild", None), "id", "") or ""), is_bot=bool(getattr(member, "bot", False)),
                before_channel=str(getattr(before.channel, "id", "") or ""), after_channel=str(getattr(after.channel, "id", "") or ""))
            if self.voice is None:
                return
            try:
                hour = time.localtime().tm_hour
                quiet = (hour >= self.voice.presence.config.quiet_start
                         or hour < self.voice.presence.config.quiet_end)
                self.voice.observe_voice_state(
                    user_id=str(getattr(member, "id", "")),
                    is_bot=bool(getattr(member, "bot", False)),
                    before_channel=str(getattr(before.channel, "id", "") or ""),
                    after_channel=str(getattr(after.channel, "id", "") or ""),
                    quiet=quiet)
            except Exception as exc:                      # noqa: BLE001 - 在場決策不該斷連線
                self._log(f"voice-state: 在場決策失敗 {type(exc).__name__}: {exc}")

        @client.event
        async def on_message(message):                    # noqa: ANN001, ANN202
            channel_id = str(getattr(message.channel, "id", "") or "")
            author_id = str(getattr(message.author, "id", "") or "")
            # 名冊與「解除延續」要**先做**：bot（Hermes）的回話也要記進來，
            # 否則「主人 ↔ 另一個 bot」的來回會被當成在跟她講（owner 2026-10-27 回報）。
            self._roster[author_id] = str(getattr(message.author, "display_name", "")
                                          or getattr(message.author, "name", "") or author_id)
            if getattr(message.author, "bot", False) \
                    and (client.user is None or author_id != str(client.user.id)):
                self.addressee.note_other_speaker(channel_id, author_id)
            if message.author.bot:
                return
            if client.user is not None and message.author.id == client.user.id:
                return
            in_thread = isinstance(message.channel, discord.Thread)
            # thread 裡只回主人：這台 Hermes 也在同一串回話，兩隻 bot 一起回會雙重覆蓋。
            if self.config.ignore_threads and in_thread \
                    and not self.router.is_owner(str(message.author.id)):
                return
            is_dm = message.guild is None
            mentioned = bool(client.user and client.user in message.mentions)
            # ① 引用回覆她的訊息（Discord 的 reply）：這就是在對她說，不必再 @ 一次
            reply_to_me = await self._is_reply_to_me(message, client)
            # ② 這句話在對誰說：收件人狀態機（`runtime/addressee.py`）。
            #    明確叫她（@／引用她／文字叫名字）⇒ 對她說，並記住「這個人正在跟她講」；
            #    叫的是別人／回的是別人 ⇒ 對別人說，且解除延續；
            #    沒有明確信號但**同一個人剛在跟她講**（窗口內）⇒ 算延續。
            #    舊的「她剛說完 + 沒 @ 別人」不算了——那會讓主人跟別人／別的 bot 講話時她插話。
            reply_other_id = "" if reply_to_me else await self._reply_target_author_id(message, client)
            addressee, addressee_reason = self._addressee_for(
                message, client, channel_id=channel_id, author_id=author_id,
                mentioned=mentioned, reply_to_me=reply_to_me, reply_other_id=reply_other_id)
            addressed = is_dm or addressee == "entity"
            continuation = addressed and addressee_reason in {"engaged", "named"}
            self.router.last_channel = str(getattr(message.channel, "id", ""))
            # 這一行只給觀測日誌用（與 `handle()` 內同一條判定，不參與決策）。
            # ⚠️ 2026-10-24 實測：以前這裡直接引用 `handle()` 的局部變數 `condition`
            # ⇒ 每則進站訊息都在寫日誌那一刻 NameError（discord.py 只記成
            # `Ignoring exception in on_message`）——bot 在線、心跳照發、回話全無。
            condition = classify(message.content or "",
                                 addressed=bool(mentioned or reply_to_me))
            # 一行觀測：live 驗收時「有沒有收到、收到誰的」全靠它（帶 bot 名＝多顆 bot 才分得出）
            self._log(f"discord[{getattr(self.config, 'default_entity', '?')}]: "
                      f"收到 {message.author}（{message.author.id}）"
                      f" ch={self.router.last_channel} dm={is_dm} "
                      f"mention={mentioned} reply_to_me={reply_to_me} "
                      f"addressee={addressee}/{addressee_reason} addressed={addressed} "
                      f"continuation={continuation} len={len(message.content or '')} "
                      f"condition={condition} substance={has_substance(message.content or '')}")
            loop = asyncio.get_running_loop()
            early: dict = {}
            sent_text = False

            def _send_text_now(body: str) -> None:
                """模型吐完就貼字（不等語音合成）。實際送出排進事件迴圈。"""
                if early.get("future") is not None or not str(body or "").strip():
                    return
                early["future"] = asyncio.run_coroutine_threadsafe(
                    message.reply(content=str(body)[:1900], mention_author=False), loop)

            try:
                result = await asyncio.to_thread(
                    self.handle, message.content, str(message.author.id),
                    guild=not is_dm, mentioned=mentioned, is_dm=is_dm,
                    channel_id=channel_id, reply_to_me=reply_to_me,
                    continuation=continuation,
                    addressed=addressed, addressee_reason=addressee_reason,
                    on_text=_send_text_now if self.config.text_first else None)
            except Exception as exc:                      # noqa: BLE001 - 一句失敗不該斷線
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._log(f"discord: 處理失敗 {self._last_error}")
                return
            if early.get("future") is not None:
                try:
                    await asyncio.wrap_future(early["future"])   # 文字先發要真的送出去
                    sent_text = True
                except Exception as exc:                          # noqa: BLE001
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    self._log(f"discord: 文字先發送出失敗 {self._last_error}")
            if not result.get("ok"):
                return
            try:
                if self.config.reply_with_audio and result.get("audio"):
                    if sent_text:
                        # 文字已經貼過 ⇒ 這則只補聲音，不再重複文字
                        await message.channel.send(
                            file=discord.File(io.BytesIO(result["audio"]),
                                              filename="reply.wav"))
                    else:
                        await message.reply(content=str(result["text"])[:1900],
                                            file=discord.File(io.BytesIO(result["audio"]),
                                                              filename="reply.wav"),
                                            mention_author=False)
                elif not sent_text:
                    await message.reply(content=str(result["text"])[:1900],
                                        mention_author=False)
                # 委任出圖／她送檔案：與文字回覆同一條選路（私聊就落在 DM）
                await self._send_attachments(message.channel, result, discord)
                # 她真的說出去了才記時間（回話失敗不算「剛說完」）
                self._note_spoke(channel_id)
            except Exception as exc:                      # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._log(f"discord: 回話失敗 {self._last_error}")

        return client

    async def _send_photos(self, channel, photos, discord) -> None:
        """把委任出圖的成品當附件貼出去（`photos` 是 `[{path, name}]`）。"""
        for photo in photos or []:
            path = str(photo.get("path") or "")
            if not path:
                continue
            await channel.send(file=discord.File(path,
                                                 filename=str(photo.get("name") or "image.png")))

    async def _send_attachments(self, channel, result, discord) -> None:
        """圖（委任出圖）先貼，檔案（file.send）後貼——兩者都只認「本機路徑＋檔名」。"""
        await self._send_photos(channel, result.get("photos") or [], discord)
        await self._send_files(channel, result.get("files") or [], discord)

    async def _send_files(self, channel, files, discord) -> None:
        """把 `file.send` 的產物當附件貼出去（`files` 是 `[{path, name}]`）。"""
        for item in files or []:
            path = str(item.get("path") or "")
            if not path:
                continue
            await channel.send(file=discord.File(path, filename=str(item.get("name") or "file.bin")))

    def _remember_loop(self) -> None:
        try:
            import asyncio
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def start(self) -> bool:
        if not self.config.enabled:
            return False
        if self._thread is not None:
            return True
        import discord                                    # 延遲載入：不用 Discord 就不佔資源
        client = self._make_client(discord, message_content=True)
        self._client = client

        def runner() -> None:
            """連線。缺 `MESSAGE_CONTENT` 特權意圖時把原因與修法寫進 health（不假裝能用）。"""
            try:
                client.run(self.config.token, log_handler=None)
            except discord.PrivilegedIntentsRequired as exc:
                self._last_error = (
                    "message_content_intent_required：到 Discord Developer Portal → "
                    f"Applications/{self.config.app_id or '<app>'}/Bot 打開 "
                    "「MESSAGE CONTENT INTENT」，否则读不到频道里的文字。原始错误："
                    f"{str(exc)[:120]}")
                self._log(f"discord: {self._last_error}")
            except Exception as exc:                      # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._log(f"discord: 連線結束 {self._last_error}")

        self._thread = threading.Thread(target=runner, daemon=True, name="discord-source")
        self._thread.start()
        return True

    def stop(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(client.close(), client.loop)
        except Exception:                                # noqa: BLE001
            pass
        self._thread = None
