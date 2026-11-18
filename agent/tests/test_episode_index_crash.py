#!/usr/bin/env python3
"""崩潰後封片序號不再重號（2026-10-27）。

背景：`thread.episode_index` 是每條線自己的序號，封片當下只寫進 `episodes.jsonl`，
要等這一輪結束 `threads.persist()` 才落進 `threads.json`。中間崩潰的話，
重啟後同一條線會再用一次同一個序號 —— id 已由開始時刻短哈希分開，但 `index` 會重號。
修法：啟動時 `EpisodeArchive.reconcile_indexes()` 把序號抬到檔案裡的最大值。

這支刻意模擬「崩潰」：封一片 → 關掉 → 手動把 threads.json 的 episode_index 倒回去 → 重開。
"""

from __future__ import annotations

import json
import pathlib
import tempfile

from demo_common import *  # noqa: F401,F403

from agent.brain.domain.ids import ThreadKey


def owner_key(entity: str = "mira") -> ThreadKey:
    return ThreadKey(scope="owner", entity=entity, scene="together", purpose="chat")


class TestEpisodeIndexSurvivesCrash(unittest.TestCase):
    def test_restart_raises_the_index_back_to_the_archive_top(self):
        root = tempfile.mkdtemp(prefix="ba-ep-index-")
        first = AppState(build(brain_delay=0, data_dir=root,
                                step_duration=0.02, life_pause=0.005))
        try:
            first.send("第一句", entity="mira", principal="owner")
            first.send("第二句", entity="mira", principal="owner")
            sealed = first.seal(entity="mira", principal="owner")
            self.assertTrue(sealed["ok"], sealed)
            self.assertEqual([episode.index for episode in first.app.archive.all()], [1])
            thread = first.app.threads.current_for(owner_key())
            assert thread is not None, "封完片應該還看得到那條線"
            thread_id = thread.id
        finally:
            first.shutdown()

        # 模擬崩潰：封片已落檔，但 threads.json 的序號沒存到
        path = pathlib.Path(root) / "threads.json"
        stored = json.loads(path.read_text(encoding="utf-8"))
        for row in stored["threads"]:
            if row.get("id") == thread_id:
                row["episode_index"] = 0
        path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")

        second = AppState(build(brain_delay=0, data_dir=root,
                                 step_duration=0.02, life_pause=0.005))
        try:
            thread = second.app.threads.current_for(owner_key())
            assert thread is not None, "重啟後應該還看得到那條線"
            self.assertEqual(thread.episode_index, 1, "啟動時要把序號抬回封片檔的最大值")
            second.send("第三句", entity="mira", principal="owner")
            sealed = second.seal(entity="mira", principal="owner")
            self.assertTrue(sealed["ok"], sealed)
            indexes = [episode.index for episode in second.app.archive.all()]
            self.assertEqual(sorted(indexes), [1, 2], "新的一集不能再叫第 1 集")
            self.assertEqual(len({episode.id for episode in second.app.archive.all()}), 2)
        finally:
            second.shutdown()


if __name__ == "__main__":
    unittest.main()
