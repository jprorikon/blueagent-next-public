"""源本體：一則 Discord 訊息怎麼變成她的一輪（含客人、旁聽、回覆判定）。"""

from __future__ import annotations

import os
import threading
import time
from agent.brain.domain.social_condition import NORMATIVE, classify, has_substance
from agent.brain.runtime.addressee import ENTITY as ADDRESSEE_ENTITY
from agent.brain.runtime.addressee import AddresseeTracker
from agent.brain.runtime.voice.vc_presence import parse_voice_command

from agent.adapter.discord_source.config import DiscordConfig
from agent.adapter.discord_source.routing import DiscordRouter
from agent.adapter.discord_source.speech import DiscordSpeech
from agent.adapter.discord_source.transport import DiscordTransport


class DiscordSource(DiscordTransport, DiscordSpeech):
    """把 discord.py 的事件接到 `AppState`。"""

    def __init__(self, config: DiscordConfig, state, *, log=None) -> None:
        self.config = config
        self.state = state
        self.router = DiscordRouter(config)
        self._log = log or (lambda _message: None)
        #: 語音頻道（`discord_voice.DiscordVoiceSession`）；None＝沒開 VC。
        self.voice = None
        self._client = None
        self._thread: threading.Thread | None = None
        self._last_error = ""
        self._replies = 0
        self._loop = None
        #: 她最近一次在各頻道說話的時間（continuation 判斷用）。
        self._spoke_at: dict = {}
        #: 群聊「這句話在對誰說」的狀態機（`runtime/addressee.py`，2026-10-27）。
        self.addressee = AddresseeTracker(
            window_s=float(getattr(config, "followup_window_s", 180.0) or 180.0))
        #: 頻道裡看過的發言者（id → 顯示名）：沒有 members intent 也能認出「在叫別人」。
        self._roster: dict = {}










    def _guest_principal(self, discord_id: str) -> str:
        """已批准 guest 的 principal；沒批准＝陌生人（回空字串）。"""
        store = getattr(getattr(self.state, "app", None), "guest_store", None)
        if store is None:
            return ""
        found = store.by_discord_id(discord_id)
        return found.principal_id if found is not None else ""

    # ── 可測的核心：一句話進來 → 一句話出去 ──────────────────────────────────
    @staticmethod
    def _source_for(principal: str, guild: bool, voice: bool) -> str:
        """這一輪的來源標籤（空字串＝交給 `routing_for`：群聊是 `discord:<principal>`）。

        ⚠️ 私訊一定要標成 Discord（複檢 2026-10-29）：不標的話 `routing_for` 只看 `group`，
        非群聊一律預設成 `phone:<principal>` ⇒ 私訊的履歷混進 App 的對話紀錄、
        她在私訊裡拿到 App 才有的表情包工具、「他剛在哪裡說話」也被記成 App。
        owner 2026-10-29：**上下文共用，但各管線的履歷只出現在各自管線**——
        線的 key 不看來源，所以改標籤不會把上下文拆開。
        """
        if voice:
            return "discord:vc"
        return "" if guild else f"discord:dm:{principal}"

    def handle(self, text: str, author_id: str, *, guild: bool, mentioned: bool,
               is_dm: bool = False, channel_id: str = "", force_addressed: bool = False,
               reply_to_me: bool = False, continuation: bool = False,
               addressed: bool | None = None, addressee_reason: str = "",
               on_text=None) -> dict:
        """`on_text(reply)`：模型一吐完就回呼（文字先發），語音合成不必等。"""
        cleaned = self.router.clean_text(text)
        if not cleaned:
            return {"ok": False, "reason": "empty"}
        # 白名單閘（owner 2026-10-22）：只認主人與 invite→approve 過的 guest；陌生人連 ingest 都不做。
        principal = self.router.principal_for(author_id) or self._guest_principal(author_id)
        if not principal:
            self._log(f"discord: 白名單外 {author_id} → 不回（ignored）")
            return {"ok": False, "ignored": True, "reason": "not_whitelisted"}
        # 主人可以直接叫她進／退語音房（確定性命令：模型只會回「好」，不會真的動）
        if principal == "owner" and self.voice is not None:
            command = parse_voice_command(cleaned)
            if command:
                return self._voice_command(command, entity=self.router.entity_for(cleaned))
        entity = self.router.entity_for(cleaned)
        # 主人在 Discord 也是 work（與 HTTP 閘一致：owner 默認 work、guest 一律 chat）
        mode = "work" if principal == "owner" else "chat"
        if force_addressed and mode == "work":
            # 語音頻道 + 要幹活 ⇒ 「先短回一句，再把真正的工作排普通優先級」
            result = self.state.send_voice(
                cleaned, entity=entity, principal=principal, mode="work",
                source="discord:vc", want_voice=bool(self.config.reply_with_audio),
                on_followup=(lambda follow: self._post_followup(channel_id, follow,
                                                                speak=True))
                if channel_id else None,
                # 後續完整答案：每合成好一句就播一句（不再等整段）
                on_speech_frame=self.publish_speech_frame if self._vc_stream_play() else None)
            frames = list(result.get("frames") or [])
            self._replies += 1
            # 逐句播（owner 2026-10-22 第 ④ 項）：短回覆通常只有一句，但這條路回的是
            # 「合併後才播」；逐句播打開時這裡直接排隊播掉，回傳 audio 留空避免 `_process` 再播一次。
            if self._vc_stream_play():
                played = sum(1 for frame in frames if self.publish_speech_frame(frame))
                if played:
                    return {"ok": True, "principal": principal, "entity": entity,
                            "text": self.state.reply_text(result),
                            "audio": b"", "frames": len(frames), "streamed": played,
                            "following_up": bool(result.get("following_up")), "mode": mode}
            # ⚠️ 這裡曾經寫死 `"audio": b""` ⇒ 語音頻道的回覆**只合成、沒播出**
            # （2026-10-22 實測：`turns.jsonl` 有 speech=4，但他一個字都沒聽到）。
            # 語音源的短回覆是「直接 send()」那條路，沒有 `handle()` 下面的 frame 迴圈，
            # 所以要自己從 frames 裡把 wav 撿回來。
            return {"ok": True, "principal": principal, "entity": entity,
                    "text": self.state.reply_text(result),
                    "audio": self.audio_from_frames(frames), "frames": len(frames),
                    "following_up": bool(result.get("following_up")), "mode": mode}
        # 多人頻道**不在這裡判「回不回」**（owner 2026-10-22：這是多人環境，要用注意力原則）。
        # 這裡只送事實：是不是 group、有沒有被 @、有沒有叫到名字；答案由 Router/AttentionGate 給。
        # ⚠ 2026-10-22 補：「在回她的話」不一定會 @ 她。三種都算在對她說——
        #   ① @ 她 ② **引用回覆她的訊息**（Discord reply）③ 她剛說完、接著講（continuation，時間窗內）。
        #   不補的話 `addressed_to=""` ⇒ 群聊只旁聽 ⇒ 他回她「她似乎沒收到」。
        # P3（owner 2026-10-22）：群裡分得出「他在問事」和「他在公開表態」。
        # 只有後者會讓她閉嘴——normative 的時候搶著回答最不像人。
        #   ① 被點名（@／引用回覆她）⇒ 一定算對她說；
        #   ② 只是「她剛說完，他接著講」⇒ 若那句是**公開表態**（沒有問句、帶評價）就不算。
        condition = classify(text, addressed=bool(mentioned or reply_to_me))
        if addressed is None:
            # 舊路徑（測試／沒接狀態機的呼叫端）：保留 2026-10-22 的規則。
            # ⚠ 主人例外在群裡是錯的（owner 2026-10-27：他跟名單外的人／另一個 bot 講話她也插話），
            #   所以現在預設由 transport 的收件人狀態機給答案；這條只剩回退用。
            owner_speaking = str(author_id) in set(getattr(self.config, "owner_ids", ()) or ())
            continuation_ok = continuation and (condition != NORMATIVE or owner_speaking)
            env_norm_gate = str(os.environ.get("BLUEAGENT_GROUP_NORM_GATE") or "1").strip().lower() \
                not in {"0", "off", "false", "no"}
            if not env_norm_gate:
                continuation_ok = continuation
            answered = bool(mentioned or force_addressed or reply_to_me or continuation_ok)
        else:
            # 收件人狀態機的結論（`runtime/addressee.py`）：只有「明確對她說」或「同一人延續」才是 True
            answered = bool(addressed or force_addressed)
        del addressee_reason                       # 只留給日誌，決策已由上面那句表達
        hint = self.router.addressed_hint(cleaned, mentioned=mentioned, is_dm=is_dm)
        reply = ""
        audio = b""
        clips: list[bytes] = []
        frames = 0
        streamed = 0
        #: 語音房逐句播放（owner 2026-10-22 第 ④ 項）；文字源一律合併。
        stream_play = bool(force_addressed) and self._vc_stream_play()
        photos: list[dict] = []
        files: list[dict] = []
        observed = False
        decision = {}
        for kind, payload in self.state.send_stream(
                text=cleaned, entity=entity, principal=principal, group=bool(guild),
                source_override=self._source_for(principal, guild, force_addressed),
                mention=bool(mentioned),
                # `addressed_to="entity"`＝在對她說；`unknown`＝查過但不是對她（2026-10-22 的坑）
                addressed_to=("entity" if answered else ("unknown" if guild else "")),
                # 她本來就是「聲音」的角色：Discord 回話預設連語音一起出（wav 當附件）
                mode=mode, want_voice=bool(self.config.reply_with_audio)):
            if kind == "result":
                observed = bool(payload.get("observed"))
                decision = dict(payload.get("decision") or {})
                continue
            if kind == "error":
                self._last_error = str(payload.get("error") or "")
                return {"ok": False, "reason": "error", "error": self._last_error}
            if kind != "frame":
                continue
            frames += 1
            # ⚠️ 這裡有兩層：外層是 SSE 幀 {kind, payload, thread, …}，內層才是
            # {kind: done|speech, text/audio}。取錯一層的症狀是「回了但內容是空的」
            # ——2026-10-22 實機就是這樣回了「（她沒有回話）」。
            body = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
            inner = str(payload.get("kind") or "")
            if inner == "done":
                reply = str(body.get("text") or "")
                if reply and on_text is not None:
                    try:
                        on_text(reply)              # 文字先發（owner 2026-10-24）
                    except Exception as exc:        # noqa: BLE001 - 先發失敗不能擋語音
                        self._log(f"discord: 文字先發失敗（{type(exc).__name__}: {exc}）")
                    on_text = None                  # 只發一次
            elif inner in ("photo", "file"):           # 委任出圖／她送檔案：與文字同一條選路
                self._collect_media_frame(inner, body, photos, files)
            elif inner == "speech" and body.get("audio"):
                wav = self._decode_audio(body["audio"])
                # **邊生成邊播**（owner 2026-10-22 第 ④ 項）：語音房裡每合成好一句就排進播放佇列，
                # 不再等整段回覆合成完才一起播——首句延遲＝第一句生成＋第一句合成。
                # 文字源（非 VC）沒有辦法串流，仍然合併成一個檔當附件。
                if stream_play:
                    try:
                        if self.voice is not None and self.voice.publish_audio(wav):
                            streamed += 1
                            continue
                    except Exception as exc:            # noqa: BLE001 - 播不出來就退回合併那條
                        self._log(f"vc: 逐句播放失敗（{type(exc).__name__}: {exc}）⇒ 退回合併")
                clips.append(wav)
        if observed:
            # 旁聽：記下來、不出聲、不回話（AttentionGate 的 OBSERVE 分支）
            return {"ok": False, "reason": "observed", "principal": principal,
                    "entity": entity, "engage": decision.get("engage"),
                    "frames": frames}
        if not reply:
            return {"ok": False, "reason": "no_reply", "principal": principal,
                    "entity": entity, "engage": decision.get("engage"),
                    "frames": frames}
        audio = self.merge_wavs(clips)
        self._replies += 1
        return {"ok": True, "principal": principal, "entity": entity, "text": reply,
                "audio": audio, "photos": photos, "files": files, "frames": frames,
                "engage": decision.get("engage"),
                "clips": len(clips), "streamed": streamed}

    def _collect_photo(self, body: dict, photos: list) -> None:
        """`photo` 幀 → 本機檔案（Discord 要上傳檔案本體；端末拿不到路徑）。"""
        path = self._photo_path(str(body.get("id") or ""))
        if path is not None:
            photos.append({"path": path, "name": str(body.get("name") or "")})

    def _collect_media_frame(self, kind: str, body: dict, photos: list, files: list) -> None:
        """`photo`／`file` 幀 → 本機檔案（兩個都靠 id 反查，只差查哪個 store）。"""
        if kind == "file":
            self._collect_file(body, files)
        else:
            self._collect_photo(body, photos)

    def _collect_file(self, body: dict, files: list) -> None:
        """`file` 幀 → 本機檔案（Discord 要上傳檔案本體，跟 photo 同一招）。"""
        path = self._file_path(str(body.get("id") or ""))
        if path is not None:
            files.append({"path": path, "name": str(body.get("name") or "")})

    def _file_path(self, file_id: str):
        """file id → 本機檔案路徑（`FileStore`）；拿不到回 None（那就只是一句話）。"""
        if not file_id:
            return None
        store = getattr(self.state, "files", None) or getattr(self.state.app, "files", None)
        path = store.path_of(file_id) if store is not None else None
        return str(path) if path is not None else None

    def _photo_path(self, media_id: str):
        """media id → 本機檔案路徑（Discord 要上傳檔案本體）。拿不到就 None。"""
        if not media_id:
            return None
        store = getattr(self.state, "media", None) or getattr(self.state.app, "media", None)
        path = store.path_of(media_id) if store is not None else None
        return str(path) if path is not None else None







    def _note_spoke(self, channel_id: str) -> None:
        """記下「她剛在這條頻道說話」的時間（continuation 判斷用）。"""
        if channel_id:
            self._spoke_at[str(channel_id)] = time.time()

    async def _is_reply_to_me(self, message, client) -> bool:
        """這則訊息是不是**引用回覆她自己的訊息**（Discord 的 reply，不必再 @）。"""
        return await self._reply_target_author_id(message, client) == _user_id(client)

    async def _reply_target_author_id(self, message, client) -> str:
        """這則訊息引用回覆的是**誰**（作者 id）；不是回覆就回空字串。

        收件人狀態機要靠它區分「回她」與「回別人」——只判 `reply_to_me` 不夠：
        他回另一個人的訊息時，她也不該插話（owner 2026-10-27）。
        """
        reference = getattr(message, "reference", None)
        if reference is None:
            return ""
        target = getattr(message, "referenced_message", None)
        if target is None:
            try:
                target = await message.channel.fetch_message(reference.message_id)
            except Exception:                            # noqa: BLE001 - 刪掉的訊息／沒權限
                target = None
        author = getattr(target, "author", None)
        return str(getattr(author, "id", "") or "") if author is not None else ""

    def health(self) -> dict:
        client = self._client
        return {"ok": bool(client is not None and not client.is_closed()),
                "user": str(getattr(client, "user", "") or ""),
                "guild": self.config.guild_id, "channel": self.config.channel_id,
                "owners": sorted(self.config.owner_ids), "replies": self._replies,
                "error": self._last_error}


def build_discord(state, *, env=None, log=None) -> DiscordSource | None:
    values = env if env is not None else {}
    config = DiscordConfig.from_env(values)
    if not config.enabled:
        return None
    return DiscordSource(config, state, log=log)


def _user_id(client) -> str:
    """這顆 bot 自己的 user id（`client.user.id`）；還沒連上就回空字串。"""
    return str(getattr(getattr(client, "user", None), "id", "") or "")


__all__ = ["DiscordSource", "build_discord"]
