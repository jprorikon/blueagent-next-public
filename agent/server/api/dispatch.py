"""POST 分派：路徑 → 處理函式（含 owner-only 前綴的統一閘）。"""

from __future__ import annotations

from agent.brain.compose.state import AppState
from agent.brain.domain.auth import AuthContext, Role

from agent.server.api.accounts import (create_account, import_accounts, revoke_account,
                                 rotate_account)
from agent.server.api.common import _owner_required
from agent.server.api.games import get_self_task, post_game, post_trpg_act, post_trpg_enter, post_trpg_say
from agent.server.api.guests import post_guest_action, post_guest_join
from agent.server.api.owner import (post_activity, post_command, post_command_cancel, post_owner_proactive,
                                    post_owner_stance, post_seal, post_voice)
from agent.server.api.system import post_metrics, post_reset
from agent.server.api.turns import post_location, post_send
from agent.server.api.tasks import post_tasks_comment


def dispatch_post(state: AppState, path: str, body: dict,
                  auth: AuthContext | None = None, ip: str = "",
                  source: str = "", external_id: str = "") -> tuple[int, dict]:
    if path.startswith(("/api/owner/", "/api/activity/", "/api/voice/")) or path in {
            "/api/seal", "/api/reset"}:
        denied = _owner_required(auth)
        if denied is not None:
            return denied
        if auth is not None:
            state.app.auth.audit("owner.action", auth=auth, path=path, ip=ip)
    if path in ("/api/pair/start", "/api/pair/claim", "/api/devices/revoke"):
        # 多設備配對（owner 2026-11-16）：claim 不用登入（新端末還沒有 token），按 IP 節流
        from agent.server.api import pairing
        if path == "/api/pair/claim":
            return pairing.post_pair_claim(state, body, ip=ip)
        if path == "/api/pair/start":
            return pairing.post_pair_start(state, body, auth=auth)
        return pairing.post_device_revoke(state, body, auth=auth)
    if path == "/api/accounts":
        return create_account(state, body, auth=auth)
    if path == "/api/accounts/import":
        return import_accounts(state, body, auth=auth)
    if path.startswith("/api/accounts/"):
        tail = path[len("/api/accounts/"):].strip("/")
        account_id, _, action = tail.partition("/")
        if action == "rotate":
            return rotate_account(state, account_id, auth=auth)
        if action == "revoke":
            return revoke_account(state, account_id, auth=auth)
        return 404, {"error": "not_found"}
    if path in ("/api/admin/identities/link", "/api/admin/identities/unlink"):
        # 腦端管理頁：外部帳號綁定（核心帳號在腦端，owner 2026-11-09）
        from agent.server.api.admin import post_link
        return post_link(state, body, auth=auth, unlink=path.endswith("/unlink"))
    if path == "/api/guests/join":
        return post_guest_join(state, body, ip=ip)
    if path == "/api/owner/proactive":
        return post_owner_proactive(state, body, auth=auth)
    if path == "/api/tasks/comment":
        # 主人在她的任務上留批注（owner 2026-11-03）：只加批注，不改狀態
        return post_tasks_comment(state, body, auth=auth)
    if path == "/api/location":
        return post_location(state, body, auth=auth, ip=ip, source=source)
    if path == "/api/send":
        return post_send(state, body, auth=auth, ip=ip, source=source,
                         external_id=external_id)
    if path.startswith("/api/activity/"):
        return post_activity(state, path.rsplit("/", 1)[-1], body, auth=auth)
    if path.startswith("/api/voice/"):
        return post_voice(state, path.rsplit("/", 1)[-1], body, auth=auth, source=source)
    if path.startswith("/api/owner/guests/"):
        return post_guest_action(state, path, body, auth=auth)
    if path == "/api/seal":
        return post_seal(state, body, auth=auth)
    if path == "/api/owner/stances":
        return post_owner_stance(state, body, auth=auth)
    if path == "/api/metrics":
        return post_metrics(state, auth=auth)
    if path == "/api/self/next":
        return get_self_task(state, body, auth=auth)
    if path == "/api/trpg/enter":
        return post_trpg_enter(state, body, auth=auth)
    if path == "/api/trpg/say":
        return post_trpg_say(state, body, auth=auth)
    if path == "/api/trpg/act":
        return post_trpg_act(state, body, auth=auth)
    if path == "/api/game":
        return post_game(state, body, auth=auth)
    if path == "/api/command":
        return post_command(state, body, auth=auth)
    if path == "/api/command/cancel":
        return post_command_cancel(state, body, auth=auth)
    if path == "/api/reset":
        return post_reset(state, auth=auth)
    return 404, {"error": "not found"}
