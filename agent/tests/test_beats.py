#!/usr/bin/env python3
"""心跳留痕（`runtime/inner/beats.py`）：每一跳一行，事後能回指「第 N 跳」。"""

from __future__ import annotations

import json
import tempfile
import unittest

from agent.brain.runtime.inner.beats import BeatLog
from agent.brain.runtime.heartbeat import Heartbeat


class TestBeatLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = BeatLog(self.tmp.name)

    def test_mark_and_tail(self):
        for tick in range(1, 6):
            self.log.mark("mira", tick=tick, at=1000.0 + tick, interval_s=30.0)
        rows = self.log.tail("mira", limit=3)
        self.assertEqual([row["tick"] for row in rows], [3, 4, 5])
        self.assertEqual(rows[-1]["interval_s"], 30.0)

    def test_tick_at_finds_the_beat_before_a_moment(self):
        for tick in range(1, 6):
            self.log.mark("mira", tick=tick, at=1000.0 + tick * 30, interval_s=30.0)
        self.assertEqual(self.log.tick_at("mira", 1000.0 + 3 * 30 + 5), 3)
        self.assertEqual(self.log.tick_at("mira", 0.0), 0)

    def test_missing_file_is_empty(self):
        self.assertEqual(self.log.tail("aster"), [])
        self.assertEqual(self.log.tick_at("aster", 123.0), 0)

    def test_file_is_trimmed_when_it_grows(self):
        log = BeatLog(self.tmp.name, max_bytes=200)
        for tick in range(1, 40):
            log.mark("nori", tick=tick, at=1000.0 + tick, interval_s=60.0)
        rows = log.tail("nori", limit=100)
        self.assertLess(len(rows), 39, "超過上限要只留後半")
        self.assertEqual(rows[-1]["tick"], 39)


class FakeAffect:
    def advance(self, entity):
        return {}

    def mood(self, entity, *, at=None):
        return {"label": "ふつう", "propensity": 0.0, "social_need": 0.0, "arousal": 0.3}


class FakeGate:
    def allows(self, entity, *, now, principal):
        return True, "ok"


class TestHeartbeatMarksBeats(unittest.TestCase):
    def test_each_tick_leaves_a_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = BeatLog(tmp)
            beat = Heartbeat(entity="mira", affect=FakeAffect(), gate=FakeGate(),
                             interval_s=30.0, beats=log)
            for _ in range(3):
                beat.tick()
            rows = log.tail("mira")
            self.assertEqual([row["tick"] for row in rows], [1, 2, 3])

    def test_restart_resumes_tick_numbering(self):
        """★ 重啟後跳號接著數（以前歸零 ⇒「N 跳後」的預約要從 0 重數、留痕跳號重複）。"""
        with tempfile.TemporaryDirectory() as tmp:
            log = BeatLog(tmp)
            first = Heartbeat(entity="mira", affect=FakeAffect(), gate=FakeGate(),
                              interval_s=30.0, beats=log)
            for _ in range(5):
                first.tick()
            again = Heartbeat(entity="mira", affect=FakeAffect(), gate=FakeGate(),
                              interval_s=30.0, beats=log)
            self.assertEqual(again.ticks, 5)
            again.tick()
            self.assertEqual([row["tick"] for row in log.tail("mira")], [1, 2, 3, 4, 5, 6])

    def test_resume_reads_last_spoken_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = BeatLog(tmp)
            for tick in range(1, 8):
                log.mark("mira", tick=tick, at=float(tick), interval_s=30.0)
            log.mark("mira", tick=4, at=4.5, interval_s=30.0, spoke=True)
            self.assertEqual(log.resume("mira"), (7, 4))
            self.assertEqual(log.resume("aster"), (0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
