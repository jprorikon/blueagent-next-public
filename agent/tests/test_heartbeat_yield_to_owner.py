#!/usr/bin/env python3
"""心跳要**給主人的對話讓路**（owner 2026-11-02：「工作可以讓路」）。

病歷（2026-11-02 22:08 實測）：主人在 App 打一句，`turns.jsonl` 寫著
`recall 154ms → subagents 5904ms → brain 38ms`、`first_token=null`，出口是**空字串**。
真因不是模型壞了：主腦那台引擎**只有一條推論 slot**，心跳每一跳都可能調三次模型
（心情定位／內心獨白／組織台詞），把他的回合排到 deadline 之外，主腦階段只剩 38 ms。

判據（這一支守的）：
  ① 忙的時候**不碰模型**（compose／moodline／monologue 一次都不准叫）；
  ② 但本地推進照走（情緒衰減、留痕、預算累積）——讓路不等於心跳停擺；
  ③ 他剛說完的 `quiet_s` 內也不要立刻插一句；
  ④ `force=True`（主人手動叫她說一句）不受讓路影響；
  ⑤ `busy` 自己拋錯 ⇒ 當成「沒人在說話」（讓路是優化，不能反過來把心跳卡死）；
  ⑥ 多角色時**各問各的**（他跟ミラ說話，不該讓アスター的心跳閉嘴）。
"""

from __future__ import annotations

import tempfile
import time
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.runtime.heartbeat import Heartbeat, build_heartbeats
from agent.brain.runtime.inner.affect import AffectService
from agent.brain.runtime.proactive import ProactiveGate


def affect_service() -> AffectService:
    return AffectService(tempfile.mkdtemp(prefix="ba-yield-"), enabled=True)


def warm(affect: AffectService, entity: str) -> None:
    noted = affect.note_message(entity, "ねえ", speaker="owner")
    affect.appraise(entity, kind="pleasure", delta_v=0.9, delta_a=0.5, confidence=0.9,
                    source_event_id=noted["event_id"])


class _Recorder:
    """假的心情定位／獨白／組句器：只記「有沒有被叫」，不做任何事。"""

    def __init__(self) -> None:
        self.composed = 0
        self.moodline = 0
        self.monologue = 0
        self.monologue_due = 0

    # compose(entity, mood, due=…) -> str（契約就是三參數；舊的兩參數版本會被 TypeError 退回）
    def compose(self, entity, mood, due=()) -> str:
        self.composed += 1
        return "ねえ、ちょっといい？"

    # 與 MoodLine.maybe_read 同一個簽名（少一個關鍵字，心跳的 except 會把它靜默吞掉）
    def maybe_read(self, entity, mood, *, at=None, events=(), force=False, age_scale=1.0):
        self.moodline += 1
        return None

    # monologue.due(entity, at=…) / monologue.beat(entity, facts=…, at=…)
    def due(self, entity, *, at=None, scale=1.0) -> bool:   # 與 Monologue.due 同一個簽名
        # 回 False ⇒ 這一跳走 `compose`（組句）那條，才驗得到「有沒有叫模型組句」。
        # 獨白本身有沒有被叫，用 `monologue_due` 另外數（讓路時它也不該被叫）。
        self.monologue_due += 1
        return False

    def beat(self, entity, *, facts=(), at=None) -> dict:
        self.monologue += 1
        return {"action": "talk", "text": "ちょっと聞いて"}


class TestHeartbeatYieldsToOwner(unittest.TestCase):
    def setUp(self) -> None:
        self.affect = affect_service()
        warm(self.affect, "mira")
        self.gate = ProactiveGate(min_gap_s=0, daily_max=99, quiet_start=0, quiet_end=0)
        self.sent: list[dict] = []
        self.rec = _Recorder()
        self.now = time.time()
        self.talking = True
        self.beat = Heartbeat(
            entity="mira", affect=self.affect, gate=self.gate,
            publish=lambda who, frame: self.sent.append(frame),
            compose=self.rec.compose, moodline=self.rec.maybe_read,
            monologue=self.rec,
            threshold=0.0, clock=lambda: self.now, quiet_s=8.0,
            busy=lambda name: self.talking)
        # `_apply_arousal_pacing()` 每跳會把 threshold 重算成 0.30–0.50（arousal 低 = 0.50），
        # 這裡要驗的是「讓路」，不是門檻 ⇒ 關掉 arousal 調制，維持 threshold=0。
        self.beat.arousal_pacing = False

    def test_busy_means_no_model_calls(self):
        self.assertEqual(self.beat.tick(), [], "主人在說話時這一跳不該開口")
        self.assertEqual(self.rec.composed, 0, "讓路的重點是**不要調模型**（組句）")
        self.assertEqual(self.rec.moodline, 0)
        self.assertEqual(self.rec.monologue, 0)
        self.assertEqual(self.rec.monologue_due, 0, "讓路時連獨白都不該跑")
        self.assertEqual(self.beat.last_skip_reason, "owner_talking")

    def test_local_progress_still_happens(self):
        self.beat.tick()
        self.assertGreaterEqual(self.beat.ticks, 1, "心跳的計數／留痕要照走")
        self.assertIsNotNone(self.affect.snapshot("mira"),
                             "情緒推進不受讓路影響")

    def test_quiet_tail_after_owner_stops(self):
        self.beat.tick()                       # 忙 ⇒ 讓路，記下尾巴
        self.talking = False
        self.now += 5.0                        # 尾巴（8 s）還沒過
        self.assertEqual(self.beat.tick(), [], "剛說完不要立刻插一句")
        self.assertEqual(self.rec.composed, 0)
        self.now += 10.0                       # 尾巴過了
        self.assertTrue(self.beat.tick(), "尾巴過了就該恢復正常")
        self.assertGreaterEqual(self.rec.composed, 1)

    def test_force_ignores_yield(self):
        self.beat.tick()                       # 先讓一次，讓尾巴成立
        spoken = self.beat.tick(force=True)
        self.assertTrue(spoken, "主人手動叫她說一句，不該被讓路擋掉")
        self.assertGreaterEqual(self.rec.composed, 1)

    def test_busy_that_raises_is_treated_as_idle(self):
        beat = Heartbeat(entity="mira", affect=self.affect, gate=self.gate,
                         publish=lambda who, frame: self.sent.append(frame),
                         compose=self.rec.compose, threshold=0.0,
                         clock=lambda: self.now,
                         busy=lambda name: (_ for _ in ()).throw(RuntimeError("boom")))
        beat.arousal_pacing = False
        self.assertTrue(beat.tick(), "問不到就當沒人說話（fail-open）")
        self.assertEqual(self.rec.composed, 1)

    def test_each_entity_asks_its_own(self):
        asked: list[str] = []

        def busy(name: str) -> bool:
            asked.append(str(name))
            return str(name) == "mira"

        beats = build_heartbeats(["mira", "aster"], affect=self.affect, gate=self.gate,
                                 publish=lambda who, frame: self.sent.append(frame),
                                 compose=self.rec.compose, threshold=0.0,
                                 busy=busy)
        self.assertEqual(beats.get("mira").tick(), [], "他在跟ミラ說話")
        self.assertIn("mira", asked)
        # aster 不該被ミラ的對話拖住（她的心跳自己問自己的）
        beats.get("aster").tick()
        self.assertIn("aster", asked)


if __name__ == "__main__":
    unittest.main()
