"""動作片段（`bamotion/1`）的離線驗收：庫、挑選規則、`motion` 幀、`/api/motion/<id>` 的語意。

對應 `docs/PLAN-motion-kimodo-20261031.md` §4（格式）／§5.1–§5.5（庫／挑選／端點）。
不碰網路、不碰 GPU：片段用合成的（T-pose 四元數＝identity）。
"""

from __future__ import annotations

import base64
import json
import pathlib
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from agent.brain.compose.motion import MotionPlanner, tag_for          # noqa: E402
from agent.brain.media.motion import (                            # noqa: E402
    BODY_JOINTS, MotionLibrary, clip_digest, problems,
)
from agent.server.api.motion import get_motion, list_motion                # noqa: E402


def make_clip(*, tag: str, frames: int = 4, fps: int = 30, hips_shift: float = 0.0) -> dict:
    """合成一段合法片段：所有關節 identity（T-pose），Hips 平移 `hips_shift` 當區分用。"""
    joints = list(BODY_JOINTS)
    quat = (0.0, 0.0, 0.0, 1.0)
    rot = struct.pack(f"<{frames * len(joints) * 4}f", *[c for _ in range(frames * len(joints)) for c in quat])
    hips = struct.pack(f"<{frames * 3}f", *[v for _ in range(frames) for v in (hips_shift, 1.0, 0.0)])
    return {
        "format": "bamotion/1",
        "id": clip_digest(rot, hips, fps),
        "tag": tag,
        "prompt": f"test clip for {tag}",
        "model": "test",
        "seed": 1,
        "fps": fps,
        "frames": frames,
        "joints": joints,
        "rotations": base64.b64encode(rot).decode("ascii"),
        "hips": base64.b64encode(hips).decode("ascii"),
        "hipsRestHeight": 1.0,
    }


class Auth:
    def __init__(self, chat: bool = True) -> None:
        self.chat = chat

    def can(self, capability: str) -> bool:
        return self.chat and capability == "chat"


class App:
    def __init__(self, library) -> None:
        self.motion = library


class State:
    def __init__(self, library) -> None:
        self.app = App(library)


def write_clip(root: pathlib.Path, clip: dict) -> pathlib.Path:
    path = root / f"{clip['id']}.json"
    path.write_text(json.dumps(clip, ensure_ascii=False), encoding="utf-8")
    return path


class TestMotionLibrary(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.library = MotionLibrary(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_synthesised_clip_passes_validation(self):
        self.assertEqual(problems(make_clip(tag="greet")), [])

    def test_validation_catches_wrong_id_and_short_payload(self):
        clip = make_clip(tag="greet")
        clip["id"] = "0" * 16
        self.assertTrue(any("內容雜湊" in e for e in problems(clip)))
        clip = make_clip(tag="greet")
        clip["rotations"] = base64.b64encode(b"\x00" * 16).decode("ascii")
        self.assertTrue(any("rotations 長度" in e for e in problems(clip)))

    def test_empty_library_is_not_an_error(self):
        self.assertEqual(self.library.count(), 0)
        self.assertEqual(self.library.tags(), {})
        self.assertIsNone(self.library.get("nope"))
        self.assertIsNone(self.library.pick("greet"))

    def test_loads_clips_and_derives_tags_without_index(self):
        clip = make_clip(tag="greet")
        write_clip(self.root, clip)
        self.assertEqual(self.library.count(), 1)
        self.assertEqual(self.library.tags(), {"greet": [clip["id"]]})
        self.assertEqual(self.library.get(clip["id"])["tag"], "greet")

    def test_index_order_wins_and_unknown_ids_are_dropped(self):
        first, second = make_clip(tag="greet"), make_clip(tag="greet", hips_shift=0.1)
        write_clip(self.root, first)
        write_clip(self.root, second)
        (self.root / "index.json").write_text(json.dumps(
            {"tags": {"greet": [second["id"], first["id"], "deadbeefdeadbeef"]}}), encoding="utf-8")
        self.assertEqual(self.library.tags()["greet"], [second["id"], first["id"]])

    def test_pick_rotates_between_seeds(self):
        a, b = make_clip(tag="greet"), make_clip(tag="greet", hips_shift=0.2)
        write_clip(self.root, a)
        write_clip(self.root, b)
        ordered = self.library.tags()["greet"]
        self.assertEqual(len(ordered), 2)
        first = self.library.pick("greet", entity="mira")
        second = self.library.pick("greet", entity="mira")     # 同一 tag 連兩輪 ⇒ 換一個
        self.assertEqual(first["id"], ordered[0])
        self.assertEqual(second["id"], ordered[1])

    def test_reload_sees_new_files(self):
        self.assertEqual(self.library.count(), 0)
        write_clip(self.root, make_clip(tag="nod"))
        self.assertEqual(self.library.count(), 1)


class TestTagRules(unittest.TestCase):
    def test_known_phrases(self):
        self.assertEqual(tag_for("おはよう、オーナー"), "greet")
        self.assertEqual(tag_for("おはよ、オーナー。うふふ"), "greet")    # 口語省「う」：不能被「うふふ」搶走
        self.assertEqual(tag_for("うん、そうだね"), "nod")
        self.assertEqual(tag_for("ごめんね…"), "bow")
        self.assertEqual(tag_for("えっ、うそ"), "surprised")

    def test_no_match_means_no_motion(self):
        self.assertIsNone(tag_for(""))
        self.assertIsNone(tag_for("今日は静かな一日だった"))


class TestMotionPlanner(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.library = MotionLibrary(self.root)
        write_clip(self.root, make_clip(tag="greet", frames=90))
        self.planner = MotionPlanner(self.library)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_frame_shape_follows_contract(self):
        frame = self.planner.frame(entity="mira", text="おはよう", source="phone")
        self.assertIsNotNone(frame)
        self.assertEqual(set(frame), {"id", "tag", "url", "hash", "seconds", "entity"})
        self.assertEqual(frame["tag"], "greet")
        self.assertEqual(frame["url"], f"/api/motion/{frame['id']}")
        self.assertEqual(frame["hash"], frame["id"])
        self.assertAlmostEqual(frame["seconds"], 3.0, places=3)

    def test_discord_and_call_turns_do_not_get_motion(self):
        self.assertIsNone(self.planner.frame(entity="mira", text="おはよう", source="discord:vc"))
        self.assertIsNone(self.planner.frame(entity="mira", text="おはよう", source="phone:call"))
        self.assertIsNone(self.planner.frame(entity="mira", text="おはよう", source="trpg"))
        self.assertIsNone(self.planner.frame(entity="mira", text="おはよう", source="heartbeat"))

    def test_empty_source_counts_as_app_turn(self):
        """App 的 POST /api/turn 沒帶 `X-Source`（2026-10-31 實測）⇒ 空字串也要發動作。"""
        frame = self.planner.frame(entity="mira", text="おはよう", source="")
        self.assertIsNotNone(frame)
        self.assertEqual(frame["tag"], "greet")
        self.assertIsNotNone(self.planner.frame(entity="mira", text="おはよう", source="api:image"))

    def test_empty_library_or_disabled_planner_is_fail_open(self):
        empty = MotionPlanner(MotionLibrary(self.root / "nothing"))
        self.assertIsNone(empty.frame(entity="mira", text="おはよう", source="phone"))
        off = MotionPlanner(self.library, enabled=False)
        self.assertIsNone(off.frame(entity="mira", text="おはよう", source="phone"))


class TestMotionApi(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.library = MotionLibrary(self.root)
        self.clip = make_clip(tag="greet")
        write_clip(self.root, self.clip)
        self.state = State(self.library)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_get_returns_clip_and_404_for_unknown(self):
        status, payload = get_motion(self.state, self.clip["id"], auth=Auth())
        self.assertEqual(status, 200)
        self.assertEqual(payload["id"], self.clip["id"])
        status, payload = get_motion(self.state, "0" * 16, auth=Auth())
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "motion_not_found")

    def test_requires_chat_capability(self):
        self.assertEqual(get_motion(self.state, self.clip["id"], auth=None)[0], 401)
        self.assertEqual(get_motion(self.state, self.clip["id"], auth=Auth(chat=False))[0], 403)

    def test_list_by_tag_and_total(self):
        status, payload = list_motion(self.state, {}, auth=Auth())
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 1)
        status, payload = list_motion(self.state, {"tag": ["greet"]}, auth=Auth())
        self.assertEqual(payload["ids"], [self.clip["id"]])
        self.assertEqual(list_motion(self.state, {"tag": ["bogus"]}, auth=Auth())[1]["ids"], [])


if __name__ == "__main__":
    unittest.main()
