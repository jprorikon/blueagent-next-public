#!/usr/bin/env python3
"""S4-d：App adapter 的授权字节可直接进入既有 VisionStage。"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.stages.intake import VisionStage


class AppMediaPipelineCase(unittest.TestCase):
    def test_vision_accepts_bytes_without_agent_private_path(self):
        env = types.SimpleNamespace(
            event=types.SimpleNamespace(payload={
                "text": "これ見て",
                "media_list": [{"id": "m0123456789abcdef", "bytes": b"authorized-bytes",
                                "mime": "image/webp"}]}),
            decision=types.SimpleNamespace(entity="aster"), meta={}, text="（画像）")
        with mock.patch("agent.brain.media.vision.describe", return_value="青い写真") as describe:
            VisionStage().run(env, None)
        describe.assert_called_once_with(b"authorized-bytes", extra="これ見て", mime="image/webp")
        self.assertIn("青い写真", env.text)
        self.assertIn("オーナーの言葉：これ見て", env.text)
        self.assertEqual(env.meta["image_path"], "")
        self.assertNotIn("path", repr(env.event.payload["media_list"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
