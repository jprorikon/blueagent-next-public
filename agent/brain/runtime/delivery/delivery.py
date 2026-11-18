"""她主动说话时**走哪条信道**（owner 2026-10-22 问「选择信道上你怎么计划的」）。

原則：

  ⓪ **主動訊息（她主動想對主人說的）一律：App → Discord DM，永不進群**
     （owner 2026-10-22：「想對主人說的話最優先去 app，其次 discord 私聊；
      只有在 discord 群裡被言及時才走注意力算法在群內發言」）
  ① 直接回覆才「回到他剛剛說話的地方」（`last_seen ≤ 15 分鐘`）——同一處說回同一處。
  ② 其餘按親密度降級：**App SSE → Discord DM →（只有 public 內容才准）群頻道**。
  ③ **私事不准進群**：`visibility="private"` 的內容只能走 App／DM。
  ④ **一次只投一條**：App 和 Discord 同時響不是雙保險，是兩次打擾。
  ⑤ **送失敗降級一次**：DM 關了就走 App；都不通就記 log，不重試、不廣播。

「能不能送到」由通道自己回答（`available()`）：
  · App：這條 SSE 長連現在有沒有他的訂閱者
  · Discord：bot 在線，且知道他的 Discord id（DM）／有設定的文字頻道（群）
"""

from __future__ import annotations

import threading
import time

from agent.brain.domain.client_end import PHONE

#: 多久之內他說話算「還在同一個地方聊」（秒）。
RECENT_S = 15 * 60


class Channel:
    """一條信道的介面。`send()` 回 True＝送到了。"""

    name = "channel"
    #: 能不能放「私事」（private log）。群頻道要設 False。
    allows_private = True

    def available(self, principal: str) -> bool:            # pragma: no cover - 介面
        raise NotImplementedError

    def send(self, principal: str, frame: dict) -> bool:     # pragma: no cover - 介面
        raise NotImplementedError


class AppChannel(Channel):
    """App 的 `GET /api/events` 長連。"""

    name = "app"
    allows_private = True

    def __init__(self, bus, *, log=None) -> None:
        self.bus = bus
        self._log = log or (lambda _message: None)

    def available(self, principal: str) -> bool:
        counts = getattr(self.bus, "counts", None)
        if counts is None:
            return True
        try:
            # 只有「有訂閱者」才算送到得到；沒開 App 就不要假裝推了。
            # 主動訊息沒有來源 ⇒ 只送手機（owner 2026-11-01：MR 自己一條管道）⇒ 只算手機的訂閱
            return bool(self.bus.publish.__self__ is not None) and bool(
                (counts(PHONE) or {}).get("subscribers", 0))
        except Exception:                                  # noqa: BLE001
            return True

    def send(self, principal: str, frame: dict) -> bool:
        try:
            delivered = self.bus.publish(principal, frame)
        except Exception as exc:                           # noqa: BLE001
            self._log(f"delivery: app 送出失敗（{type(exc).__name__}: {exc}）")
            return False
        return bool(delivered)


class DiscordChannel(Channel):
    """Discord：**公開的事先貼群，私事才 DM**（owner 2026-10-22 改）。

    原先是「先 DM 再群」——結果她每次主動開口都變成戳私聊，看起來像推銷。
    現在：`private` ⇒ 只 DM（群裡不放私事）；`public` ⇒ 先貼群，貼不上才退 DM。
    """

    name = "discord"
    allows_private = True          # DM 可以；群那條自己再擋（`allow_group`）

    def __init__(self, source, *, allow_group: bool = True, log=None) -> None:
        self.source = source
        #: `{entity: source}`（多顆 bot）：主動訊息要用**那個角色自己的** bot 出去，
        #: 不然 aster 的話會從 mira 的頭像冒出來（owner 2026-10-27）。
        self.sources = {}
        self.allow_group = bool(allow_group)
        self._log = log or (lambda _message: None)

    def source_for(self, frame: dict | None = None):
        """這一則該用哪顆 bot 送：看 frame 的 `entity`，沒有就回主源。"""
        entity = str((frame or {}).get("entity") or "")
        return (self.sources or {}).get(entity) or self.source

    def available(self, principal: str) -> bool:
        source = self.source
        if source is None or getattr(source, "_client", None) is None:
            return False
        return bool(getattr(source, "can_dm", None) and source.can_dm(principal)) \
            or (self.allow_group and bool(getattr(source.config, "channel_id", "")))

    def send(self, principal: str, frame: dict) -> bool:
        source = self.source_for(frame)          # 多顆 bot：用這一則所屬角色的那顆
        if source is None:
            return False
        text = str(frame.get("text") or "").strip()
        if not text:
            return False
        private = str(frame.get("visibility") or "public") == "private"
        # 寂寞／親密的話**只走私聊**（owner 2026-10-22：「寂寞发话最好还是私聊或者将来手机 app」）。
        # 這類話貼在群裡等於當眾撒嬌——場合錯了，所以直接跳過群那條。
        intimate = str(frame.get("urgency") or "") == "lonely" \
            or str(frame.get("visibility") or "").lower() in {"private", "intimate"}
        # 主動訊息（她自己想講的）**一律只走私聊**：群裡只有「被言及」時才說話
        if str(frame.get("kind") or "") == "proactive" or frame.get("proactive"):
            intimate = True
        try:
            # 公開的事先在群裡說（她主動開口應該像「在同一個房間講話」，不是私訊你）
            if (not private and not intimate and self.allow_group
                    and getattr(source.config, "channel_id", "")):
                if source.send_text(text):
                    return True
            if getattr(source, "can_dm", None) and source.can_dm(principal):
                if source.send_dm(principal, text):
                    return True
        except Exception as exc:                           # noqa: BLE001
            self._log(f"delivery: discord 送出失敗（{type(exc).__name__}: {exc}）")
        return False


class DiscordVoiceChannel(Channel):
    """Discord 語音：**寂寞度高時**她會想直接說話（owner 2026-10-22）。

    機器人能做的「單人電話」只有兩種，都寫在這裡：
      · 他**已經在同一個 VC** ⇒ 直接在裡面說（最自然，先走這條）
      · 他不在 ⇒ 先用 DM 丟一句「先に来てるね」（邀他進來），人來了再開口
    （Discord 不允許 bot 主動「撥號」給個人，這是平台限制，不是我省事。）
    """

    name = "voice"
    allows_private = True

    def __init__(self, source, *, log=None) -> None:
        self.source = source
        #: `{entity: source}`（多顆 bot）：她要用**自己的**那顆進 VC／講話
        self.sources = {}
        self._log = log or (lambda _message: None)

    def source_for(self, frame: dict | None = None):
        entity = str((frame or {}).get("entity") or "")
        return (self.sources or {}).get(entity) or self.source

    def _voice(self):
        return self._voice_for(None)

    def _voice_for(self, frame: dict | None):
        source = self.source_for(frame)
        return getattr(source, "voice", None) if source is not None else None

    def available(self, principal: str) -> bool:
        voice = self._voice()
        if voice is None or not getattr(voice, "joined", False):
            return False
        # 有他在同一個頻道才值得開口；不在就只能發邀請
        return bool(getattr(voice, "humans_present", lambda: False)())

    def send(self, principal: str, frame: dict) -> bool:
        source = self.source_for(frame)
        voice = self._voice_for(frame)
        text = str(frame.get("text") or "").strip()
        if voice is None or not text:
            return False
        try:
            if getattr(voice, "humans_present", lambda: False)():
                voice.say(text)                     # VC 裡直接說
                return True
            # 他不在：發一句邀請（走 DM 或設定的文字頻道），聲音等她進房再說
            invited = False
            if getattr(source, "can_dm", None) and source.can_dm(principal):
                invited = bool(source.send_dm(principal, f"（先に声の方に来てるね）{text}"))
            if not invited and str(frame.get("visibility") or "public") != "private":
                invited = bool(getattr(source, "send_text", lambda _t: False)(text))
            return invited
        except Exception as exc:                   # noqa: BLE001
            self._log(f"delivery: voice 送出失敗（{type(exc).__name__}: {exc}）")
            return False


class ChannelRouter:
    """挑一條信道送出；`order` 是降級順序。"""

    def __init__(self, channels, *, recent_s: float = RECENT_S, log=None,
                 clock=time.time) -> None:
        self.channels = {channel.name: channel for channel in channels}
        self.order = [channel.name for channel in channels]
        self.recent_s = max(0.0, float(recent_s))
        self._log = log or (lambda _message: None)
        self._clock = clock
        self._seen: dict[str, tuple[str, float]] = {}
        self._lock = threading.RLock()
        self.stats = {"sent": 0, "failed": 0, "dropped": 0}

    # ── 他剛剛在哪裡說話 ────────────────────────────────────────────────────
    def note_seen(self, principal: str, channel: str, *, at: float | None = None) -> None:
        with self._lock:
            self._seen[str(principal)] = (str(channel), float(at if at is not None else self._clock()))

    def last_channel(self, principal: str, *, at: float | None = None) -> str:
        with self._lock:
            found = self._seen.get(str(principal))
        if not found:
            return ""
        name, when = found
        now = float(at if at is not None else self._clock())
        return name if (now - when) <= self.recent_s else ""

    # ── 送 ──────────────────────────────────────────────────────────────────
    def candidates(self, principal: str, frame: dict) -> list[str]:
        private = str(frame.get("visibility") or "public") == "private"
        # 主動訊息：固定 App（先走 App Server 會話，平台有歷史與已讀；不通才退回舊 SSE）→ DM，且**不進群**
        if str(frame.get("kind") or "") == "proactive" or frame.get("proactive"):
            names = [name for name in ("app_server", "app", "discord") if name in self.channels]
            if not names:                       # 名字萬一不同，退回「非群」的通道
                names = [name for name, ch in self.channels.items() if getattr(ch, "allows_private", False)]
            return names
        recent = self.last_channel(principal)
        names: list[str] = []
        # 寂寞到某個程度 ⇒ 語音優先（她想要「人聲」，不是文字）
        if (frame.get("want_voice") or str(frame.get("urgency") or "") == "lonely") \
                and "voice" in self.channels:
            names.append("voice")
        if recent and recent in self.channels:
            names.append(recent)
        names.extend(name for name in self.order if name not in names)
        return [name for name in names
                if (self.channels[name].allows_private or not private)]

    def send(self, principal: str, frame: dict) -> dict:
        for name in self.candidates(principal, frame):
            channel = self.channels[name]
            if not channel.available(principal):
                continue
            if channel.send(principal, frame):
                self.stats["sent"] += 1
                self.note_seen(principal, name)
                return {"sent": True, "channel": name}
            self.stats["failed"] += 1
            self._log(f"delivery: {name} 送不出去 ⇒ 降級")
        self.stats["dropped"] += 1
        return {"sent": False, "channel": ""}

    def snapshot(self) -> dict:
        return {"stats": dict(self.stats), "order": list(self.order), "recent_s": self.recent_s}


__all__ = ["AppChannel", "Channel", "ChannelRouter", "DiscordChannel",
           "DiscordVoiceChannel", "RECENT_S"]
