#!/usr/bin/env python3
"""「她現在什麼心情、為什麼」——AEM 讀數（owner 2026-10-24）。

背景：主人說「我總覺得她一直挺開心的」。查下去發現不是錯覺：
  · 評分（appraisal）幾乎只給正增量：delta_valence 平均 +0.37、只有 2% 是負的；
  · delta_arousal 是 `max(0, intensity) * 0.8` ⇒ **永遠不會是負的**（只升不降）；
  · valence 用 48 小時平均 ⇒ 一直被墊在 0.3~0.5，於是標籤長期停在「ごきげん」。
所以「她一直很開心」是**量表本身只會往上推**，不是她演技不好。

判據：
  ① timeline 把每次評分的增量與原因攤開（時間／種類／dv／da）
  ② stats 算得出「負值比例」與「arousal 有幾次是負的」——兩個都是 0 就證明量表單向
  ③ 沒有 AEM 時要乾淨回空（不能炸）
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from agent.brain.runtime.inner.affect import AffectService  # noqa: E402


def build(enabled: bool = True) -> AffectService:
    root = pathlib.Path(tempfile.mkdtemp())
    return AffectService(root, enabled=enabled)


def feed(service: AffectService, entity: str = "mira"):
    noted = service.note_message(entity, "ただいま", speaker="owner")
    event_id = str(noted.get("event_id") or "")
    for kind, delta_v, delta_a in (("pleasure", 0.4, 0.5), ("frustration", -0.2, 0.3)):
        service.note_message(entity, kind, speaker="owner")     # 事件載體（AEM 要求來源存在）
        note = service.note_message(entity, kind, speaker="owner")
        service.appraise(entity, kind=kind, delta_v=delta_v, delta_a=delta_a,
                         source_event_id=str(note.get("event_id") or event_id),
                         target_type="owner")


class TestTimeline(unittest.TestCase):
    def test_episodes_are_listed_newest_first(self):
        service = build()
        feed(service)
        rows = service.timeline("mira", limit=10)
        self.assertTrue(rows, "應該要看得到評分流水")
        self.assertEqual({row["kind"] for row in rows} & {"pleasure", "frustration"},
                         {"pleasure", "frustration"})
        self.assertEqual([row["at"] for row in rows],
                         sorted((row["at"] for row in rows), reverse=True))

    def test_stats_expose_one_sided_drift(self):
        service = build()
        feed(service)
        stats = service.stats("mira")
        self.assertGreaterEqual(stats["count"], 2)
        self.assertIn("negative_ratio", stats)
        self.assertIn("arousal_negative", stats)
        self.assertEqual(stats["kinds"].get("pleasure", 0) >= 1, True)

    def test_disabled_service_returns_empty(self):
        service = build(enabled=False)
        self.assertEqual(service.timeline("mira"), [])
        self.assertEqual(service.stats("mira"), {"hours": 24.0, "count": 0})


class TestMoodEndpointAuth(unittest.TestCase):
    def test_requires_owner(self):
        """不是主人 ⇒ **403**（token 有效但這帳號不能看她的心情）。

        ⚠️ 2026-11-02 改：從前這裡回 401，但契約（`docs/accounts-and-clients.md` §4）的 401
        是「token 壞了／被撤銷 ⇒ 端末回登入頁」⇒ guest 打心情端點會被踢回登入頁。
        """
        from agent.server import api

        class Auth:
            is_owner = False
            principal = "guest"

        status, body = api.get_mood(object(), {}, auth=Auth())
        self.assertEqual(status, 403, body)
        # 沒帶 token 才是 401
        self.assertEqual(api.get_mood(object(), {}, auth=None)[0], 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
