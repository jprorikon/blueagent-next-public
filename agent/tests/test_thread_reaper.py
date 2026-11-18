"""線的回收（E4，2026-11-17）：`ThreadStore.prune()`＋`ThreadReaper`＋persist 鎖外寫檔。

線上實測 513 條活線、`threads.json` 2.48 MB、persist p95 31 ms：`reap()` 從來沒人呼叫，
過窗開新世代後舊世代一直留著。判據：
  ① 被新世代取代的舊線閒過門檻 ⇒ 關；當世代閒很久也不關（承接摘要的來源）；當世代死透（30 天）⇒ 關；
  ② 正在跑／有排隊的線絕不關；
  ③ 關了之後重啟也不會回來（寫進檔）；同一組 key 再來時世代號繼續往上、不重號；
  ④ 被關的舊線若有沒封片的尾巴：最近一天內的補封，結束時刻＝它最後活動的時間；更舊的不補；
  ⑤ 關之前把還沒進原文倉的輪次補進去；
  ⑥ persist 在鎖外序列化：寫檔時 Router 的 attach 不必等；舊快照不會蓋掉新快照；寫出來是緊湊 JSON；
  ⑦ 換世代的承接摘要照常繼承（回收不影響當世代）。
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import threading
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.brain.domain.episode import CutReason                        # noqa: E402
from agent.brain.domain.event import SourceClass                       # noqa: E402
from agent.brain.domain.ids import ThreadKey                           # noqa: E402
from agent.brain.domain.thread import TurnRecord                       # noqa: E402
from agent.brain.runtime.ops.thread_reaper import ThreadReaper             # noqa: E402
from agent.brain.runtime.threadstore import ThreadStore                # noqa: E402

HOUR = 3600.0
DAY = 24 * HOUR
WINDOW = 15 * 60.0
T0 = 1_800_000_000.0


def key(scope="owner", entity="mira", scene="together"):
    return ThreadKey(scope=scope, entity=entity, scene=scene)


def talk(store, k, at, text="やあ", *, turn="t"):
    thread = store.attach(k, now=at, window=WINDOW, lane=SourceClass.INTERACTIVE).thread
    with thread.lock:
        thread.record(TurnRecord(turn_id=f"{turn}{at}", role="user", text=text, source="phone:owner",
                                 at=at, speaker="owner"))
    return thread


class FakeRaw:
    def __init__(self):
        self.synced: list[str] = []

    def sync_thread(self, thread, *, now=None):
        self.synced.append(thread.id)
        return 0


class PruneCase(unittest.TestCase):
    def setUp(self):
        self.folder = pathlib.Path(tempfile.mkdtemp(prefix="ba-reap-"))
        self.path = self.folder / "threads.json"
        self.raw = FakeRaw()
        self.store = ThreadStore(path=self.path, raw_log=self.raw)

    def test_superseded_generation_closed_current_kept(self):
        old = talk(self.store, key(), T0)
        new = talk(self.store, key(), T0 + 2 * HOUR)          # 過窗 ⇒ 新世代
        self.assertNotEqual(old.id, new.id)
        self.assertEqual(self.store.prune(T0 + 5 * HOUR), [], "還沒到 6 小時")
        closed = self.store.prune(T0 + 7 * HOUR)
        self.assertEqual([t.id for t in closed], [old.id])
        self.assertIsNone(self.store.get(old.id))
        self.assertIs(self.store.current_for(key()), new)
        self.assertIn(old.id, self.raw.synced, "關之前要補進原文倉")
        self.assertEqual(self.store.pruned, 1)

    def test_current_generation_survives_long_idle_until_stale(self):
        only = talk(self.store, key(), T0)
        self.assertEqual(self.store.prune(T0 + 10 * DAY), [])
        self.assertIs(self.store.current_for(key()), only)
        closed = self.store.prune(T0 + 31 * DAY)
        self.assertEqual([t.id for t in closed], [only.id])
        self.assertIsNone(self.store.current_for(key()))
        again = talk(self.store, key(), T0 + 32 * DAY)
        self.assertNotEqual(again.id, only.id, "世代號不能重用")
        self.assertTrue(again.id.endswith("-2"), again.id)

    def test_busy_threads_never_closed(self):
        old = talk(self.store, key(), T0)
        new = talk(self.store, key(), T0 + 2 * HOUR)
        with old.lock:
            old.queued = 1
        closed = self.store.prune(T0 + 40 * DAY)
        self.assertEqual([t.id for t in closed], [new.id], "閒的當世代照收，有排隊的舊世代留著")
        self.assertIsNotNone(self.store.get(old.id))

    def test_pruned_threads_stay_gone_after_restart(self):
        old = talk(self.store, key(), T0)
        new = talk(self.store, key(), T0 + 2 * HOUR)
        talk(self.store, key(scope="group-tea"), T0 + 2 * HOUR)
        self.store.prune(T0 + 7 * HOUR)
        reloaded = ThreadStore(path=self.path)
        ids = {t.id for t in reloaded.threads()}
        self.assertNotIn(old.id, ids)
        self.assertIn(new.id, ids)
        self.assertEqual(len(ids), 2)

    def test_carry_summary_still_inherited_by_next_generation(self):
        first = talk(self.store, key(), T0)
        with first.lock:
            first.carry_summary = "〔ptr〕さっきは天気の話"
        second = talk(self.store, key(), T0 + HOUR)
        self.assertEqual(second.carry_summary, "〔ptr〕さっきは天気の話")
        self.store.prune(T0 + 8 * HOUR)                        # 第一代被收
        third = talk(self.store, key(), T0 + 9 * HOUR)
        self.assertEqual(third.carry_summary, "〔ptr〕さっきは天気の話", "當世代沒被收，承接照常")


class ReaperCase(unittest.TestCase):
    def setUp(self):
        self.store = ThreadStore()
        self.sealed: list[tuple[str, float, CutReason]] = []

    def seal(self, thread, ended, reason):
        self.sealed.append((thread.id, ended, reason))
        return object()

    def test_recent_tail_sealed_with_its_own_end_time_old_tail_not(self):
        recent_old = talk(self.store, key(), T0 + 20 * DAY, "最近の話")
        talk(self.store, key(), T0 + 20 * DAY + HOUR)
        ancient = talk(self.store, key(scope="guest"), T0)
        talk(self.store, key(scope="guest"), T0 + 2 * HOUR)
        reaper = ThreadReaper(self.store, seal=self.seal)
        result = reaper.run_once(now=T0 + 20 * DAY + 8 * HOUR)
        self.assertEqual(result["closed"], 2)
        self.assertEqual(result["sealed"], 1)
        self.assertEqual(self.sealed, [(recent_old.id, T0 + 20 * DAY, CutReason.IDLE)])
        self.assertNotIn(ancient.id, [row[0] for row in self.sealed], "一天以前的尾巴不補封")

    def test_seal_failure_does_not_stop_reaping(self):
        talk(self.store, key(), T0)
        talk(self.store, key(), T0 + HOUR)

        def broken(thread, ended, reason):
            raise RuntimeError("archive down")

        result = ThreadReaper(self.store, seal=broken).run_once(now=T0 + 8 * HOUR)
        self.assertEqual((result["closed"], result["sealed"], result["alive"]), (1, 0, 1))


class SupersedeCase(unittest.TestCase):
    """換世代那一刻先封舊線的尾巴（`ThreadStore.on_supersede`）：承接摘要要接到最近那一段。"""

    def setUp(self):
        from agent.brain.runtime.episodes import EpisodeArchive
        self.store = ThreadStore()
        self.archive = EpisodeArchive(summarize=lambda ep: " / ".join(turn.text for turn in ep.turns))
        self.store.on_supersede(lambda prev: self.archive.seal(prev, prev.last_active_at, CutReason.IDLE))

    def test_tail_sealed_and_carried(self):
        old = talk(self.store, key(), T0, "池袋でケーブルを買った")
        fresh = talk(self.store, key(), T0 + HOUR, "ただいま")
        self.assertNotEqual(old.id, fresh.id)
        self.assertIn("ケーブル", fresh.carry_summary)
        self.assertEqual(old.segment, [])
        episode = self.archive.latest(1)[0]
        self.assertEqual((episode.thread_id, episode.ended_at), (old.id, T0), "結束時刻用舊線最後活動")

    def test_same_generation_does_not_seal(self):
        talk(self.store, key(), T0)
        talk(self.store, key(), T0 + 60)
        self.assertEqual(self.archive.all(), ())

    def test_busy_or_broken_does_not_block_the_new_generation(self):
        old = talk(self.store, key(), T0, "まだ途中")
        with old.lock:
            old.queued = 1
        fresh = talk(self.store, key(), T0 + HOUR)
        self.assertNotEqual(old.id, fresh.id)
        self.assertEqual(self.archive.all(), (), "有排隊的不封")
        self.store.on_supersede(lambda prev: 1 / 0)
        third = talk(self.store, key(), T0 + 3 * HOUR)
        self.assertNotEqual(third.id, fresh.id)


class PersistCase(unittest.TestCase):
    def setUp(self):
        self.path = pathlib.Path(tempfile.mkdtemp(prefix="ba-persist-")) / "threads.json"
        self.store = ThreadStore(path=self.path)

    def test_compact_json(self):
        talk(self.store, key(), T0)
        self.assertTrue(self.store.persist())
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("\n", text)
        self.assertEqual(len(json.loads(text)["threads"]), 1)

    def test_attach_not_blocked_while_file_write_runs(self):
        talk(self.store, key(), T0)
        entered, release = threading.Event(), threading.Event()
        real_dumps = json.dumps

        def slow_dumps(*args, **kwargs):
            entered.set()
            release.wait(5)
            return real_dumps(*args, **kwargs)

        import agent.brain.runtime.threadstore as module
        module.json.dumps = slow_dumps
        try:
            writer = threading.Thread(target=self.store.persist)
            writer.start()
            self.assertTrue(entered.wait(5))
            module.json.dumps = real_dumps                     # attach 自己的 persist 不要再卡
            done = threading.Event()

            def attach():
                self.store.attach(key(scope="other"), now=T0, window=WINDOW, lane=SourceClass.INTERACTIVE)
                done.set()

            threading.Thread(target=attach).start()
            self.assertTrue(done.wait(2), "序列化期間 attach 被 _lock 擋住了")
        finally:
            module.json.dumps = real_dumps
            release.set()
            writer.join(5)
        body = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(body["threads"]), 2, "慢的舊快照不能蓋掉 attach 之後的新快照")


if __name__ == "__main__":
    unittest.main(verbosity=2)
