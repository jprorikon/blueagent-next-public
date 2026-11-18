"""HTTP 路由背後的處理函式（回 `(status, payload)`）。按路由群分模組；這裡把名字匯總出來，
`server/http.py` 照舊用 `api.X`。
"""

from __future__ import annotations

from agent.server.api.common import _unauthorized, _forbidden, _owner_required, _id, _participants  # noqa: F401
from agent.server.api.accounts import (get_me, list_accounts, create_account,  # noqa: F401
                                 rotate_account, revoke_account,
                                 export_accounts, import_accounts)
from agent.server.api.system import health, get_mood, get_state, post_metrics, post_reset  # noqa: F401
from agent.server.api.turns import list_turns, get_turn, get_turn_audio, prepare_send, prepare_audio, post_send, post_location, list_stickers, sticker_file  # noqa: F401
from agent.server.api.media import (prepare_image, store_max_bytes, list_media, store_upload,  # noqa: F401
                              upload_or_prepare_image)
from agent.server.api.games import get_trpg_tables, get_trpg_table, post_trpg_say, get_self_task, post_trpg_enter, post_trpg_act, post_game  # noqa: F401
from agent.server.api.guests import get_guest_status, get_guest_directory, get_owner_guests, get_whitelist, post_guest_join, post_guest_action  # noqa: F401
from agent.server.api.owner import post_activity, post_voice, post_seal, get_owner_stances, post_owner_stance, get_commands, post_command, post_command_cancel, post_owner_proactive  # noqa: F401
from agent.server.api.memory import get_memory_trace, get_memory_windows  # noqa: F401
from agent.server.api.motion import get_motion, list_motion  # noqa: F401
from agent.server.api.diary import get_diary, get_diary_day, get_diary_work  # noqa: F401
from agent.server.api.tasks import get_tasks  # noqa: F401
from agent.server.api.sessions import list_sessions  # noqa: F401
from agent.server.api.board import can_read_media  # noqa: F401
from agent.server.api.app_kick import handle as app_kick_handle, kick as app_kick, token as app_kick_token  # noqa: F401
from agent.server.api.dispatch import dispatch_post  # noqa: F401

__all__ = ["create_account", "dispatch_post", "get_commands", "get_diary", "get_diary_day", "get_diary_work", "get_guest_directory", "get_guest_status", "get_me", "get_memory_trace", "get_memory_windows", "get_mood", "get_motion", "get_owner_guests", "get_owner_stances", "get_self_task", "get_state", "get_tasks", "get_trpg_table", "get_trpg_tables", "get_turn", "get_turn_audio", "get_whitelist", "health", "list_accounts", "list_media", "list_motion", "list_sessions", "list_stickers", "list_turns", "post_activity", "post_command", "post_command_cancel", "post_game", "post_guest_action", "post_guest_join", "post_location", "post_metrics", "post_owner_proactive", "post_owner_stance", "post_reset", "post_seal", "post_send", "post_trpg_act", "post_trpg_enter", "post_trpg_say", "post_voice", "prepare_audio", "prepare_image", "prepare_send", "revoke_account", "export_accounts", "import_accounts", "rotate_account", "sticker_file", "store_max_bytes", "app_kick", "app_kick_handle", "app_kick_token"]
