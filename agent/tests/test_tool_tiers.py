"""工具與技能的分層（owner 2026-10-31：「不是每一個渠道都需要帶這麼長的工具上下文」）。

規則在 `brain/runtime/tools/channels.py`：
常駐（哪裡都有）／按渠道（`channels`）／按需解鎖（`on_demand`，`skill.view` 載入技能後才出現）。
"""

from __future__ import annotations

import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.skills import Skill, SkillLibrary
from agent.brain.runtime.tools import ToolContext, ToolRegistry, ToolResult
from agent.brain.runtime.tools.channels import channel_of


class TestChannelOf(unittest.TestCase):
    def test_sources_map_to_channels(self):
        cases = {
            "": "app", "phone:owner": "app", "api": "app", "api:audio": "app", "phone:mic": "app",
            "phone:call": "call", "voice:desk": "call", "mic:desk": "call",
            "discord:owner": "discord", "discord:group:lobby": "discord",
            "discord:vc": "discord_vc", "discord:vc-lobby": "discord_vc",
            "trpg:table-1": "trpg", "table": "trpg",
            "self": "self", "clock:tick": "clock",
        }
        for source, channel in cases.items():
            self.assertEqual(channel_of(source), channel, source)


class _Tool:
    def __init__(self, name, **attrs):
        self.name = name
        self.schema = {"type": "function", "function": {"name": name.replace(".", "_")}}
        for key, value in attrs.items():
            setattr(self, key, value)

    def run(self, ctx, arguments):
        return ToolResult(ok=True, data={"ran": self.name})


def _registry():
    registry = ToolRegistry()
    registry.register(_Tool("memory.search"))
    registry.register(_Tool("sticker.send", channels=("app",)))
    registry.register(_Tool("weather.get", on_demand=True))
    registry.register(_Tool("trpg.roll", channels=("trpg",)))
    return registry


def _names(registry, source, **kwargs):
    return [row["function"]["name"] for row in registry.schemas(source=source, **kwargs)]


class TestRegistryTiers(unittest.TestCase):
    def test_channel_and_on_demand_filter_the_list(self):
        registry = _registry()
        self.assertEqual(_names(registry, "phone:owner"), ["memory_search", "sticker_send"])
        self.assertEqual(_names(registry, "trpg:table-1"), ["memory_search", "trpg_roll"])
        self.assertEqual(_names(registry, "phone:owner", unlocked={"weather.get"}),
                         ["memory_search", "sticker_send", "weather_get"])

    def test_on_demand_is_visibility_only_but_channels_are_enforced(self):
        registry = _registry()
        ctx = ToolContext(entity="mira", principal="owner", thread_id="t", source="trpg:table-1",
                          voice=None)
        # 沒解鎖也能執行（模型猜對名字不該被擋）；渠道不對就擋
        self.assertTrue(registry.run("weather.get", {}, ctx).ok)
        result = registry.run("sticker.send", {}, ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "tool_not_allowed:sticker.send")

    def test_available_and_canonical(self):
        registry = _registry()
        self.assertTrue(registry.available("weather_get", source="discord:owner"))
        self.assertFalse(registry.available("sticker.send", source="phone:call"))
        self.assertEqual(registry.canonical("weather_get"), "weather.get")
        self.assertEqual(registry.canonical("nope"), "")
        # 不帶 source ⇒ 不看渠道（自主工作那條有自己的白名單）
        self.assertTrue(registry.allowed(registry.get("sticker.send")))


class TestSkillIndexByChannel(unittest.TestCase):
    def test_skill_is_listed_only_where_its_tools_exist(self):
        library = SkillLibrary()
        library._cache["-"] = [
            Skill(name="weather", description="天気", scope="chat", body="", requires=["tool:weather.get"]),
            Skill(name="table", description="卓", scope="chat", body="", requires=["tool:trpg.roll"]),
            Skill(name="manners", description="作法", scope="chat", body=""),
        ]
        # 快取現在要對得上目錄指紋才用（2026-11-03：她剛寫的技能下一輪就要看得到）⇒ 塞假資料也要一起塞指紋
        library._cache_stamp["-"] = library._stamp("")
        registry = _registry()

        def listed(source):
            return [skill.name for skill in library.index(
                tool_available=lambda name: registry.available(name, source=source))]

        self.assertEqual(listed("phone:owner"), ["weather", "manners"])
        self.assertEqual(listed("trpg:table-1"), ["weather", "table", "manners"])
        self.assertIn("先に", library.render_index())             # 「先に skill.view」要寫明


class TestUnlockInATurn(unittest.TestCase):
    """真的跑一輪：她先 `skill.view(weather)`，下一步的工具清單就多了 `weather_get`；量測進 turns.jsonl。"""

    def setUp(self):
        self.state = make_state()
        self.addCleanup(self.state.shutdown)
        seen: list[list[str]] = []
        self.seen = seen

        class SkillBrain:
            def run_turn(self, request):
                names = [row["function"]["name"] for row in request.meta.get("tools") or []]
                seen.append(names)
                if len(seen) == 1:
                    yield ("tool_call", {"id": "call_1", "name": "skill_view",
                                         "arguments": {"name": "weather"}})
                    yield ("done", {"text": ""})
                    return
                yield ("delta", {"text": "晴れだよ。"})
                yield ("done", {"text": "晴れだよ。", "entity": "mira", "principal": "owner",
                                "model": "test"})

        self.state.app.runtime.ports.brain = SkillBrain()

    def test_skill_view_unlocks_its_tools_for_the_next_step(self):
        list(self.state.send_stream(text="今日の天気は？", entity="mira", principal="owner"))
        self.assertGreaterEqual(len(self.seen), 2, self.seen)
        self.assertNotIn("weather_get", self.seen[0])
        self.assertIn("skill_view", self.seen[0])
        self.assertIn("weather_get", self.seen[1])

        row = self.state.app.telemetry.tail(1)[-1]
        self.assertEqual(row["tools"], len(self.seen[0]))
        self.assertGreater(row["tool_chars"], 0)
        self.assertEqual(row["tools_unlocked"], ["weather.get"])


class TestTierMetrics(unittest.TestCase):
    """`/api/metrics` 的 tool_tiers／motion：上線後拿來判斷分層與動作要不要調。"""

    def test_aggregates(self):
        from agent.brain.runtime.ops.telemetry import TurnRecorder
        rows = [
            {"tools": 7, "tool_chars": 2000, "tools_unlocked": ["weather.get"], "motion": "chosen:greet"},
            {"tools": 7, "tool_chars": 2200, "tools_unlocked": [], "motion": "rule:nod"},
            {"tools": 3, "tool_chars": 900},
            {"ms": 10},                                         # 舊紀錄：沒有量測欄位
        ]
        tiers = TurnRecorder._tool_tiers(rows)
        self.assertEqual(tiers["turns"], 3)
        self.assertEqual(tiers["tools_avg"], 5.7)
        self.assertEqual(tiers["unlock_rate"], round(1 / 3, 3))
        self.assertEqual(tiers["unlocked"], {"weather.get": 1})
        motion = TurnRecorder._motion(rows)
        self.assertEqual((motion["chosen"], motion["rule"]), (1, 1))
        self.assertEqual(motion["tags"], {"greet": 1, "nod": 1})
        self.assertEqual(TurnRecorder._tool_tiers([{"ms": 1}]), {"turns": 0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
