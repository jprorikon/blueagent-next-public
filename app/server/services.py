"""App Server 运行时装配：只组合独立模块，不 import Agent。"""

from __future__ import annotations

import threading

from app.server.accounts import AccountStore
from app.server.chat.service import ChatService
from app.server.chat.store import ChatStore
from app.server.config import AppConfig
from app.server.device_auth import Introspector
from app.server.events import EventLog
from app.server.kicker import EventsKicker
from app.server.location import LocationService
from app.server.media import MediaService
from app.server.mood import MoodService
from app.server.social.service import BoardService
from app.server.stickers import StickerService
from app.server.voice import AppVoiceService, BrainVoiceBridge
from app.server.voice_outgoing import CallNotices


class AppServices:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        # 人類 token 以 Agent 為準（多設備配對，2026-11-16）：沿用 brain URL 與既有 kick token
        self.accounts = AccountStore(config.accounts_path, introspector=Introspector(
            config.brain_url, config.kick_token, grace_s=config.auth_grace_s))
        # 平台 → Agent 的推送叫醒（留空＝關；Agent 那側會退回 20 s 兜底輪詢）
        self.kicker = EventsKicker(config.kick_url, config.kick_token)
        self.board = BoardService(config.data_root, media_owner=lambda _media_id: None)
        self.events = EventLog(self.board.store.db_path, kicker=self.kicker,
                               accounts=self.accounts)
        self.chat_store = ChatStore(self.board.store.db_path)
        self.media = MediaService(self.board.store.db_path, config.data_root,
                                  board=self.board, chat_store=self.chat_store)
        self.board.media_owner = self.media.owner
        self.chat = ChatService(self.chat_store, self.accounts, media_owner=self.media.owner,
                                media_purge=self.media.purge)
        self.mood = MoodService(self.board.store.db_path, self.accounts)
        self.location = LocationService(self.board.store.db_path, self.accounts)
        self.stickers = StickerService(config.client_assets, config.data_root)
        self.voice = AppVoiceService(
            self.accounts, BrainVoiceBridge(config.brain_url, config.voice_token))
        # 她打給主人時的 `call.incoming`／`call.updated`：寫進主人的事件流（只給他看）
        self.call_notices = CallNotices(self.board.store.db_path, events=self.events)
        self.voice.notify = self.call_notices
        # 主人沒接她的電話 ⇒ 私聊裡留一則「不在着信」（App 不在前台也看得到她打過）
        self.voice.missed = self._record_missed_call
        self.stopping = threading.Event()

    def _record_missed_call(self, session) -> None:
        self.chat.record_missed_call(session.entity, session.account_id, session.id)
        self.events.flush()                              # 背景計時器寫的：這條執行緒自己清待叫佇列

    def close(self) -> None:
        self.stopping.set()
        self.voice.close()
        self.call_notices.close()
        self.kicker.close()
        self.events.close()
        self.media.close()
        self.mood.close()
        self.location.close()
        self.chat_store.close()
        self.board.close()


__all__ = ["AppServices"]
