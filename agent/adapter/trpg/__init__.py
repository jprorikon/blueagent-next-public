"""把她放進 TRPG 桌：**桌端 kick 叫醒她 → 她看牌面 → 說一句＋宣告一個合法動作**。

為什麼是這個形狀（照 `workspace/trpg/agent_harness/agents_kick.py` 的現役做法，不另發明）：

  · 桌端只會 **kick**（HTTP POST `X-Table-Kick-Token`），MCP 只能「她主動做動作」——
    叫不醒一個待機的 agent，所以入口必須是一個 HTTP 接收器。
  · 看牌面／出手都走 MCP（`table_view` / `table_act` / `table_say`），不打 GraphQL。
  · **數值只由引擎算**：這裡只挑「allowed_actions 裡的一個」，
    被拒就把引擎的原話回給她、讓她改一手（現役實測 14 手 0 被拒）。

與現役 harness 的差別（刻意的）：

  · 台詞由**她本人**（同一條 persona／記憶／情緒）說，不是另一個「TRPG 玩家」人格；
  · 動作由**確定性規則**從她那句話裡挑（不強迫她輸出 JSON——她的人格不該被格式綁住）；
  · 她的回覆照樣進記憶與 `turns.jsonl`（source=`trpg`），玩過的局她記得住。

按職責拆開（2026-10-25 review P3）：設定（config）、MCP 協議（mcp）、動作決策（decide）、
她的心情與遊戲事件（mood）、匯報（report）、prompt（prompt）、席位本體（seat）。
`from adapter.trpg import X` 照用。
"""

from __future__ import annotations

from agent.adapter.trpg.config import TrpgConfig  # noqa: F401
from agent.adapter.trpg.mcp import TrpgMcp  # noqa: F401
from agent.adapter.trpg.decide import _ACTION_WORDS, _ACTION_ALIASES, render_view, choose_action  # noqa: F401
from agent.adapter.trpg.mood import GAME_AFFECT_SCALE, GAME_WEAR_STEP, GAME_AFFECT  # noqa: F401
from agent.adapter.trpg.seat import TrpgSeat  # noqa: F401

__all__ = ["GAME_AFFECT", "GAME_AFFECT_SCALE", "GAME_WEAR_STEP", "TrpgConfig", "TrpgMcp", "TrpgSeat", "choose_action", "render_view"]
