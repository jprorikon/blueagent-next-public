#!/usr/bin/env python3
"""REVIEW 2026-11-07 #1：App 群聊用自己的 scope／scene，不借 Discord 大厅那条线。

判据：
  ① 不传 override 时行为不变（Discord 大厅仍是 `group:discord-lobby`／`discord-lobby`）；
  ② 传了就一律用传进来的值（App 群聊 = `group:<conversation_id>`／`app-group`）；
  ③ 两个不同的会话 id 推出两条不同的 thread（不能共用一条线）；
  ④ 一对一仍然是 principal，不受影响。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent" / "tests"))

from agent.brain.compose.turns import routing_for  # noqa: E402
from agent.brain.domain.ids import ThreadKey  # noqa: E402


class RoutingScopeOverrideCase(unittest.TestCase):
    def test_default_group_routing_is_unchanged(self):
        source, scope, scene, target = routing_for("owner", True, False, "", False)
        self.assertEqual((source, scope, scene), ("discord:owner", "group:discord-lobby",
                                                  "discord-lobby"))
        self.assertEqual(target, "")

    def test_explicit_scope_and_scene_win(self):
        source, scope, scene, target = routing_for(
            "owner", True, False, "", False,
            scope="group:cnv_agent_room", scene="app-group")
        self.assertEqual(source, "discord:owner", "source 由 source_override 另行覆盖")
        self.assertEqual(scope, "group:cnv_agent_room")
        self.assertEqual(scene, "app-group")
        self.assertEqual(target, "")

    def test_two_app_rooms_do_not_share_a_thread(self):
        first = routing_for("owner", True, False, "", False,
                            scope="group:cnv_a", scene="app-group")
        second = routing_for("owner", True, False, "", False,
                             scope="group:cnv_b", scene="app-group")
        keys = {ThreadKey(scope=scope, entity="aster", scene=scene, purpose="chat").base
                for _source, scope, scene, _target in (first, second)}
        self.assertEqual(len(keys), 2, "两个 App 群聊不能落到同一条 thread")

    def test_direct_chat_ignores_group_defaults(self):
        source, scope, scene, _target = routing_for("owner", False, False, "", False,
                                                   scope="", scene="")
        self.assertEqual((source, scope, scene), ("phone:owner", "", "together"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
