"""應用狀態：對外的一個入口（HTTP／Discord／TRPG 都打這裡），把工作交給窄服務。

review.md（2026-10-25）P1：原本 628 行、什麼都自己做。現在：
  · 回合執行 → `app/turns.py` 的 `TurnService`（不起 HTTP、不建 AppState 也能測）；
  · 串流交付＋outbox → `app/streaming.py` 的 `StreamDelivery`；
  · 語音「先出聲、再幹活」→ `app/followups.py` 的 `VoiceTurns`；
  · 這裡留下：路由紀錄、快照（含 guest 過濾）、YIELD 補位、主人指令佇列、相容的委託。
`reset()` 會重建 App，所以服務在 `_wire()` 裡跟著重接。
"""

from __future__ import annotations

import os
import pathlib
import threading
import time
from collections import deque

from agent.brain.compose import activity_api, episode_api, guest_api, voice_api
from agent.brain.compose.account_service import AccountService
from agent.brain.compose.bootstrap import App, build
from agent.brain.compose.followups import VoiceTurns, same_gist as _same_gist  # noqa: F401
from agent.brain.compose.inner_service import InnerService
from agent.brain.compose.memory_service import MemoryViewService
from agent.brain.compose.snapshot import build_snapshot
from agent.brain.compose.streaming import StreamDelivery
from agent.brain.compose.trpg_service import TrpgService
from agent.brain.compose.turns import (AMBIENT_SOURCES, VOICE_SOURCES,  # noqa: F401
                                      TurnService, reply_bubbles as _reply_bubbles,
                                      reply_text as _reply_text,
                                      source_class_for as _source_class_for)
from agent.brain.runtime.owner_queue import CANCEL_HOOK, OwnerCommandQueue


class AppState:
    def __init__(self, app: App | None = None) -> None:
        self.app = app or build()
        self._lock = threading.RLock()
        self.route_log: deque[dict] = deque(maxlen=40)
        self.started_at = time.time()
        self._stop = threading.Event()
        #: 主人的工作指令佇列：同一個角色一次只跑一條，每條有編號與狀態（`GET /api/commands`）
        #: 落盤在 `<data>/owner_commands.json`（owner 2026-11-16）：重啟後排隊的照跑，跑到一半的不重跑
        data_dir = getattr(self.app, "data_dir", None)
        self.commands = OwnerCommandQueue(
            self._run_command, log=self._log_command,
            store_path=(pathlib.Path(data_dir) / "owner_commands.json") if data_dir else None,
            deliver=self._deliver_command)
        self._wire()
        self._yield_worker = threading.Thread(target=self._yield_loop, daemon=True,
                                              name="ba-yield")
        self._yield_worker.start()
        self.commands.start()

    def _wire(self) -> None:
        """依目前的 App 重接窄服務（`reset()` 換 App 後不能保留舊引用）。"""
        # 核心帳號在腦端：App／Discord 的外部帳號綁到 principal（owner 2026-11-09，`brain/app/identity.py`）。
        # 要在 TurnService 與 adapter 之前：它們都靠 `app.identity` 翻譯。
        from agent.brain.compose.identity import bind_identities
        bound = bind_identities(self.app)
        self._log_command(f"identity: 外部身分綁定 {bound}")
        self.turns = TurnService.from_app(self.app, on_decision=self._note_decision)
        self.streams = StreamDelivery(send=self.send, outbox=getattr(self.app, "outbox", None))
        self.voice_turns = VoiceTurns(send=self.send, commands=self.commands,
                                      bus=getattr(self.app, "bus", None))
        self.trpg_service = TrpgService()
        self.memory_views = MemoryViewService(self.app)
        self.account_service = AccountService(self.app)
        self.inner = InnerService(self.app)
        old_app_chat = getattr(self, "app_chat", None)
        if old_app_chat is not None:
            for adapter in old_app_chat:
                adapter.close()
        from agent.adapter.app_server import build_app_chat_adapters
        from agent.brain.compose.group_opener import GroupOpener
        # 三人房安静时她自己开场（GROUP-SELF-CHAT §2 ①）：敲门在 adapter，问她要不要说在这里
        opener = GroupOpener(brain=self.app.brain, threads=self.app.threads,
                             persona_cores=getattr(self.app, "persona_cores", None),
                             speak_lang=str(os.environ.get("BLUEAGENT_SPEAK_LANG") or "").strip().lower(),
                             log=self._log_command)
        self.app_chat = build_app_chat_adapters(self, opener=opener)
        for adapter in self.app_chat:          # 進來的 App 帳號 id 一律翻成腦端 principal
            adapter.identity = getattr(self.app, "identity", None)
            adapter.board_events.identity = adapter.identity
        # 主動消息走 App Server 會話（有歷史、有已讀；PLAN-common-ground §7.4）
        from agent.adapter.app_proactive import attach as attach_conversation_channel
        attach_conversation_channel(self.app, self.app_chat, log=self._log_command)
        # 她打給主人：`voice.call(end=phone)`／自主想打 ⇒ App 響鈴（`adapter/app_call_out.py`）
        from agent.adapter.app_call_out import attach as attach_call_out
        data_dir = getattr(self.app, "data_dir", None)
        attach_call_out(self.app, self.app_chat, log=self._log_command,
                        state_path=os.path.join(str(data_dir), "call_out.json") if data_dir else None)
        # 主人此刻方不方便被打擾（通話中／正在別處聊／剛道別；PLAN-common-ground §5.2）
        from agent.brain.compose.interruptibility import wire as wire_interruptibility
        wire_interruptibility(self.app)
        # 情感整理（PLAN-affect-reorg）：公共區反饋進心情、被 @ 的拿起率乘心情係數
        from agent.brain.compose.affect_wiring import wire as wire_affect
        wire_affect(self.app, self.app_chat)
        for adapter in self.app_chat:
            adapter.start()
        # 公开活动 → 公共区：**默认不自动发**（owner 2026-11-08：发帖与「事情收束」解耦，
        # 发帖改为她自己的动作 mcp_board_post）；BLUEAGENT_BOARD_AUTOPUBLISH=1 才回到旧行为。
        old_publisher = getattr(self, "board_publisher", None)
        if old_publisher is not None:
            old_publisher.close()
        from agent.adapter.app_board_publish import build_board_publisher
        self.board_publisher = build_board_publisher(self)
        if self.board_publisher is not None:
            self.board_publisher.start()

    def board_for(self, auth=None):
        """公共区只经 App Server；没有本地数据库或投影回退。"""
        import os
        from agent.adapter.app_board import RemoteBoardService
        from agent.adapter.app_protocol import BoardError
        if auth is not None and not auth.can("chat"):
            raise BoardError(403, "forbidden")
        base_url = str(os.environ.get("BLUEAGENT_APP_SERVER_URL") or "").strip()
        if not base_url:
            raise BoardError(503, "app_public_unconfigured")
        credential = str(getattr(auth, "credential", "") or "")
        if not credential:
            raise BoardError(401, "unauthorized")
        return RemoteBoardService(base_url, credential)

    def _note_decision(self, decision: dict) -> None:
        with self._lock:
            self.route_log.appendleft(decision)

    def _log_command(self, message: str) -> None:
        print(f"  {message}", flush=True)

    def _deliver_command(self, item: dict, result: dict) -> None:
        """重啟後恢復的指令做完了：原本的交付回呼存不了，改推到 App 的常駐 SSE（與聊天後續同一條路）。"""
        self.voice_turns.publish((result or {}).get("result") or {}, entity=str(item.get("entity") or "mira"),
                                 principal=str(item.get("principal") or "owner"),
                                 source=str(item.get("source") or "api:command"))

    def _run_command(self, text: str, *, entity: str = "", principal: str = "owner",
                     meta: dict | None = None) -> dict:
        """佇列裡真正執行的一步：就是走一次 work 回合（與語音那條路同一個入口）。"""
        extra = dict(meta or {})
        source = str(extra.pop("source", "") or "api:command")
        # 逐句串流播放：這一步的 speech 幀一合成好就交出去播，不等整段回覆跑完
        frame_hook = extra.pop("on_speech_frame", None)
        # 執行中取消（2026-11-16）：把這一輪的 `job.cancel` 交回佇列，工具迴圈在下一個邊界停
        register = extra.pop(CANCEL_HOOK, None)
        on_job = (lambda job: register(job.cancel)) if callable(register) else None
        result = self.send(text, entity=entity or "mira", principal=principal,
                           mode="work", source_override=source, followup=True,
                           on_frame=frame_hook, on_job=on_job, **extra)
        return {"text": self.reply_text(result), "result": result}

    # ── 一次輸入（委託給窄服務）──────────────────────────────────────────────
    def send(self, text: str, *, admin: bool = True, ack_first: bool | None = None,
             **kwargs) -> dict:
        """跑一輪（`TurnService`），再附上這個 principal 看得到的應用快照。"""
        result = self.turns.run(text, **kwargs)
        # 跟回合同一個 principal（無主回合沒有 principal）與同一份在場名冊
        principal = "" if kwargs.get("ownerless") else str(kwargs.get("principal", "owner"))
        result["state"] = self.snapshot(principal=principal,
                                        entity=str(kwargs.get("entity", "mira")),
                                        participants=tuple(result.get("room") or ()),
                                        admin=admin)
        return result

    @staticmethod
    def reply_text(result: dict) -> str:
        return _reply_text(result)

    @staticmethod
    def reply_bubbles(result: dict) -> list[str]:
        return _reply_bubbles(result)

    def send_voice(self, text: str, **kwargs) -> dict:
        return self.voice_turns.send_voice(text, **kwargs)

    def _followup_worker(self, text: str, entity: str, principal: str, source: str,
                         on_followup, extra: dict) -> None:
        self.voice_turns.followup_worker(text, entity, principal, source, on_followup, extra)

    def send_stream(self, **kwargs):
        return self.streams.stream(**kwargs)

    # ── 快照與控制 ──────────────────────────────────────────────────────────────
    def snapshot(self, *, principal: str = "owner", entity: str = "mira",
                 participants=None, admin: bool = True) -> dict:
        with self._lock:
            route_log = list(self.route_log)
        if not admin:
            route_log = [row for row in route_log
                         if str(row.get("principal") or "") == principal]
        payload = build_snapshot(self.app, principal=principal, entity=entity,
                                 participants=participants, route_log=route_log,
                                 started_at=self.started_at)
        payload["attention"] = self.app.attention.snapshot(entity, "", time.time())
        if not admin:
            payload["pending"] = []
            payload.pop("guests", None)
            payload["threads"] = [
                thread for thread in payload.get("threads", [])
                if thread.get("scope") == principal
                or (str(thread.get("scope") or "").startswith("group:")
                    and principal in (thread.get("participants") or []))
            ]
            social = payload.get("social") or {"relations": [], "circles": []}
            payload["social"] = {
                "relations": [row for row in social.get("relations", [])
                              if principal in (row.get("a"), row.get("b"))],
                "circles": [row for row in social.get("circles", [])
                            if principal in (row.get("members") or [])],
            }
            for key in ("episodes", "life", "voice", "scheduler",
                        "pipelines", "counts"):
                payload.pop(key, None)
            # `visible=False` 是披露判斷，不是公開原文的授權；guest 只能拿到可披露事實。
            memory = payload.get("memory")
            if isinstance(memory, dict):
                memory["facts"] = [fact for fact in memory.get("facts", [])
                                   if fact.get("visible")]
            return payload
        payload["pending"] = self.app.pending_store.list_all()
        guests = self.app.guest_store.snapshot()
        for item in guests["pending"] + guests["accounts"]:
            item.pop("token", None)
            item.pop("token_sha256", None)
            item.pop("request_secret", None)
        payload["guests"] = guests
        return payload

    def seal(self, **kwargs) -> dict:
        return episode_api.seal(self, **kwargs)

    def start_activity(self, **kwargs) -> dict:
        return activity_api.start(self, **kwargs)

    def pause_activity(self, **kwargs) -> dict:
        return activity_api.pause(self, **kwargs)

    def resume_activity(self, **kwargs) -> dict:
        return activity_api.resume(self, **kwargs)

    def reset_activity(self, **kwargs) -> dict:
        return activity_api.reset(self, **kwargs)

    def reload_activity(self, **kwargs) -> dict:
        return activity_api.reload(self, **kwargs)

    def hangup_voice(self, entity: str = "mira", call_id: str = "") -> dict:
        return voice_api.hangup(self, entity, call_id)

    # ── App 單人通話（owner 2026-10-29）────────────────────────────────────────
    def start_call(self, entity: str, principal: str, end: str = "phone") -> tuple[int, dict]:
        return voice_api.call(self, entity, principal, end)

    def ping_call(self, entity: str, call_id: str) -> tuple[int, dict]:
        return voice_api.ping(self, entity, call_id)

    def interrupt_call(self, entity: str, principal: str) -> tuple[int, dict]:
        return voice_api.interrupt(self, entity, principal)

    # ── 立场（owner 手动） ──────────────────────────────────────────────────────
    def owner_stances(self, entity: str = "mira") -> dict:
        if self.app.entities.get(entity) is None:
            raise KeyError(f"unknown entity: {entity}")
        names = {item.id: item.name for item in self.app.principals.all()}
        return {"ok": True, "entity": entity,
                "stances": self.app.stances.snapshot(entity, names=names)}

    def set_stance(self, *, entity: str, principal: str, weights=None,
                   notes=None, trust=None) -> dict:
        if self.app.entities.get(entity) is None:
            raise KeyError(f"unknown entity: {entity}")
        if self.app.principals.get(principal) is None:
            raise KeyError(f"unknown principal: {principal}")
        names = {item.id: item.name for item in self.app.principals.all()}
        stance = self.app.stances.set(entity, principal, weights=weights,
                                       notes=notes, trust=trust)
        return {"ok": True,
                "stance": self.app.stances.describe(entity, stance.principal,
                                                     names=names),
                "state": self.snapshot(principal="owner", entity=entity)}

    def _yield_loop(self) -> None:
        while not self._stop.wait(0.2):
            store = self.app.pending_store
            for item in store.due(time.time()):
                try:
                    self.send(item.text, entity=item.entity, principal=item.principal,
                              group=item.group, mention=item.mention,
                              addressed_to=item.addressed_to,
                              participants=list(item.participants), replay=True,
                              force_reply=True, request_id=f"yield-{item.id}",
                              # 回到原回合的線與來源（REVIEW 2026-11-08：以前一律落到預設路由）
                              source_override=item.source_override,
                              scope_override=item.scope_override,
                              scene_override=item.scene_override)
                except Exception as exc:               # noqa: BLE001
                    self.route_log.appendleft(
                        {"kind": "yield_error",
                         "message": f"{type(exc).__name__}: {exc}"})
                finally:
                    store.remove(item.id)

    def shutdown(self) -> None:
        self._stop.set()
        self._yield_worker.join(timeout=2.0)
        for adapter in getattr(self, "app_chat", []):
            adapter.close()
        if getattr(self, "board_publisher", None) is not None:
            self.board_publisher.close()
        for loop in list(self.app.lives.values()):
            try:
                loop.shutdown()
            except Exception:                          # noqa: BLE001 - 一條關不掉不擋其他條
                pass

    # ── Guest 加入與主人審核 ──────────────────────────────────────────────────────
    def join_guest(self, **kwargs) -> dict:
        return guest_api.join(self, **kwargs)

    def guest_status(self, secret: str) -> dict:
        return guest_api.status(self, secret)

    def guest_directory(self) -> dict:
        return guest_api.directory(self)

    def owner_guests(self) -> dict:
        return guest_api.owner_guests(self)

    def approve_guest(self, request_id: str, **kwargs) -> dict:
        return guest_api.approve(self, request_id, **kwargs)

    def reject_guest(self, request_id: str) -> dict:
        return guest_api.reject(self, request_id)

    def revoke_guest(self, request_id: str) -> dict:
        return guest_api.revoke(self, request_id)

    def reset(self) -> dict:
        self.app.pending_store.clear()
        self.app.memory.clear()
        self.app.threads.clear()
        self.app.archive.clear()
        delay = self.app.brain.delay
        root = self.app.activity_store.root.parent
        with self._lock:
            try:
                self.app.life.shutdown()
            except Exception:                      # noqa: BLE001
                pass
            self.app = build(brain_delay=delay, data_dir=root,
                              attention_config=self.app.attention.config)
            self.route_log.clear()
            self.started_at = time.time()
            self._wire()
        return self.snapshot()
