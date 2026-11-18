"""主動消息走 App Server 的會話（docs/PLAN-common-ground-20261109.md §7.4，owner 2026-11-09「改走会话」）。

以前她的主動消息走 Agent 自己的 `/api/events`：客戶端收進本地暫存、在聊天頁「交出即已讀」，
平台上沒有這一則、也沒有已讀 ⇒ 她不知道主人看到沒有。現在：

  · `AppConversationChannel`：主動消息用**她自己的帳號**經 MCP `chat.message.send` 寫進
    主人↔她的 direct 會話（`/v1/conversations/direct` 先確保會話存在）。客戶端照一般訊息收：
    未讀、背景通知、打開即標已讀都是現成的。送不出去 ⇒ 回 False，`ChannelRouter` 退回舊 SSE。
  · `chat.read.updated`（App Server 已讀游標往前走時記的事件）⇒ `ProactiveGate.note_read`。

只接主動消息（`kind=proactive`）；後續交付等其他幀不走這裡。
`BLUEAGENT_PROACTIVE_VIA_CONVERSATION=0` ⇒ 不接，回舊行為。
"""

from __future__ import annotations

import os
import threading
import time

from agent.brain.runtime.delivery.delivery import Channel


def _enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get("BLUEAGENT_PROACTIVE_VIA_CONVERSATION") or "1").strip().lower() \
        not in {"0", "off", "false", "no"}


class AppConversationChannel(Channel):
    """主動消息 → App Server 的 direct 會話（她自己的帳號送）。"""

    name = "app_server"
    allows_private = True

    def __init__(self, clients: dict, *, on_sent=None, log=None, accounts=None) -> None:
        #: `{entity: AppServerClient}`：每個角色用自己的 token
        self.clients = dict(clients or {})
        self.on_sent = on_sent
        self._log = log or (lambda _message: None)
        #: 大腦的 principal → 平台帳號 id 的翻譯表（`brain/runtime/identity/accounts.py`）。
        #: 生產上兩邊不同名（`owner` vs `acct_owner`），沒翻就整個通道靜默降級（2026-11-09 實測）。
        self.accounts = accounts
        self._conversations: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def available(self, principal: str) -> bool:
        return bool(self.clients)

    def _account(self, principal: str) -> str:
        """principal ⇒ 平台帳號 id（沒有 accounts 就原樣用）。"""
        body = str(principal or "")
        if self.accounts is None:
            return body
        try:
            return str(self.accounts.account_id_of(body) or body)
        except Exception:                                  # noqa: BLE001 - 翻譯壞了照舊送
            return body

    def _conversation(self, client, entity: str, principal: str) -> str:
        key = (str(entity), str(principal))
        with self._lock:
            found = self._conversations.get(key)
        if found:
            return found
        reply = client.open_direct(self._account(principal)) or {}
        conversation_id = str((reply.get("conversation") or reply).get("id") or "")
        if conversation_id:
            with self._lock:
                self._conversations[key] = conversation_id
        return conversation_id

    def send(self, principal: str, frame: dict) -> bool:
        if not (str(frame.get("kind") or "") == "proactive" or frame.get("proactive")):
            return False
        entity = str(frame.get("entity") or "")
        text = str(frame.get("text") or "").strip()
        client = self.clients.get(entity)
        if client is None or not text:
            return False
        at = float(frame.get("at") or time.time())
        try:
            conversation_id = self._conversation(client, entity, principal)
            if not conversation_id:
                return False
            result = client.send_message(conversation_id, [text],
                                         mutation_id=f"proactive:{entity}:{int(at * 1000)}",
                                         causation_id="")
        except Exception as exc:                          # noqa: BLE001 - 送不出去 ⇒ 退回 SSE
            self._log(f"delivery: app_server 送出失敗（{type(exc).__name__}: {exc}）⇒ 降級")
            return False
        message = (result or {}).get("message") or {}
        if self.on_sent is not None:
            try:
                self.on_sent(entity, str(principal), conversation_id=conversation_id,
                             message_id=str(message.get("id") or ""),
                             seq=int(message.get("seq") or 0), at=at)
            except Exception as exc:                      # noqa: BLE001 - 記不上不影響已送出
                self._log(f"delivery: 主動消息位置沒記上（{type(exc).__name__}）")
        return True


def make_read_handler(proactive, accounts=None):
    """回 `on_read(entity, payload)`：對方（不是她自己）在 direct 會話讀到哪 ⇒ 交給閘。

    `accounts`：平台帳號 id → 大腦 principal 的翻譯（`read` 事件的 `account_id` 是 `acct_owner`，
    閘的鍵是 `owner`；不翻就永遠對不上，2026-11-09 實測）。
    """

    def on_read(entity: str, payload: dict) -> None:
        reader = str(payload.get("account_id") or "")
        if not reader:
            return
        if accounts is not None:
            try:
                reader = str(accounts.principal_of(reader) or reader)
            except Exception:                          # noqa: BLE001 - 翻不了就用原值
                pass
        if reader == str(entity):
            return
        if str(payload.get("conversation_kind") or "direct") != "direct":
            return                      # 群聊的已讀另議（PLAN-common-ground H7）
        proactive.note_read(str(entity), reader,
                            conversation_id=str(payload.get("conversation_id") or ""),
                            seq=int(payload.get("message_seq") or 0),
                            at=float(payload.get("read_at") or 0.0) or None)

    return on_read


def attach(app, adapters, *, log=None, env=None):
    """把會話通道接進 `app.delivery`、已讀接進各 adapter。回接上的通道（沒接回 None）。"""
    delivery = getattr(app, "delivery", None)
    channels = getattr(delivery, "channels", None)
    if not isinstance(channels, dict):
        return None
    channels.pop(AppConversationChannel.name, None)      # reset() 重接時先拿掉舊的
    proactive = getattr(app, "proactive", None)
    # 腦端的身分中樞（`brain/app/identity.py`）優先；沒有才退回帳號表的翻譯
    accounts = getattr(app, "identity", None) or getattr(app, "accounts", None)
    clients = {adapter.entity: adapter.client for adapter in (adapters or ())}
    if not clients or not _enabled(env):
        return None
    channel = AppConversationChannel(
        clients, log=log, accounts=accounts,
        on_sent=getattr(proactive, "note_delivered", None))
    channels[channel.name] = channel
    if proactive is not None and hasattr(proactive, "note_read"):
        handler = make_read_handler(proactive, accounts=accounts)
        for adapter in adapters:
            adapter.on_read = handler
    return channel


__all__ = ["AppConversationChannel", "attach", "make_read_handler"]
