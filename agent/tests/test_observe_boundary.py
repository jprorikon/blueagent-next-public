#!/usr/bin/env python3
"""旁聽的邊界（owner 2026-10-22）：「不寫記憶，但進 session，當短期上下文」。

判據：
  ① 判定為旁聽（OBSERVE）⇒ **不產事實**（記憶池條數不變）；
  ② 同一句話**留在 thread 的 transcript 裡**（下一輪她看得到剛才誰說了什麼）；
  ③ 叫到她、她也回了 ⇒ 照舊寫記憶。

⚠️ 這裡直接指定注意力結論（`OBSERVE`）。為什麼不靠自然判定：注意力有「發言權租約」
（focus_lease）——她剛被叫過的一段時間內會主動接話，那是設計，不是漏洞。要驗的是
「旁聽這一支」的行為，所以把 gate 的結論固定住。
"""

from __future__ import annotations

import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.compose.state import AppState
from agent.brain.domain.attention import AttentionDecision
from agent.brain.domain.decision import Engagement


class TestObserveBoundary(unittest.TestCase):
    def setUp(self):
        # 每次用乾淨的 data/（不然上一輪寫過的事實會讓「有沒有新增」判斷失效）
        self.root = tempfile.mkdtemp(prefix="ba-observe-")
        self.app = build(brain_delay=0, data_dir=self.root,
                          step_duration=0.02, life_pause=0.005)
        self.state = AppState(self.app)
        self.addCleanup(self.app.shutdown_lives)

    def facts(self) -> int:
        """以「這個 guest 在場」的視角數事實——`snapshot()` 預設是主人視角，
        guest 的 private 事實在那裡本來就看不到（會誤判成「沒寫」）。

        ⚠️ 排除 `activity*`：生活迴圈（`LifeLoop`）是背景執行緒，會在任意時刻往記憶裡
        寫活動日誌事實；把它算進來這條斷言就會隨機器負載抖動（實測在整包測試裡偶發失敗）。
        這裡只數「對話輪沉下來的事實」。
        """
        rows = self.app.memory.snapshot({"guest-x"})["facts"]
        return len([row for row in rows
                    if not str(row.get("source") or "").startswith("activity")])

    def _force(self, action: Engagement, reason: str) -> None:
        def decide(event, thread, base, **_kwargs):
            return AttentionDecision(action=action, score=0.3, reason=reason)

        self.app.attention.decide = decide

    def test_observed_line_enters_session_but_not_memory(self):
        self._force(Engagement.OBSERVE, "test_observe")
        before = self.facts()
        result = self.state.send("今日はいい天気だね", principal="guest-x", group=True,
                                 mention=False, addressed_to="")
        self.assertTrue(result["observed"], result)
        self.assertEqual(self.facts(), before, "旁聽不該沉成事實")

        thread_id = str(result["decision"]["thread"])
        thread = self.app.threads.get(thread_id)
        self.assertIsNotNone(thread)
        self.assertTrue(any("いい天気" in (turn.text or "") for turn in thread.transcript),
                        "旁聽的話要留在 session（transcript）裡")

    def test_addressed_line_still_writes_memory(self):
        before = self.facts()
        # 「记住」是 fact_rules 的 explicit 觸發詞（日文「覚えて」不在規則裡）
        result = self.state.send("ミラ、记住我喜欢ほうじ茶", principal="guest-x",
                                 group=False, mention=False, addressed_to="entity")
        self.assertFalse(result["observed"], result)
        self.assertGreater(self.facts(), before, "叫到她、她也回了 ⇒ 照舊寫記憶")


if __name__ == "__main__":
    unittest.main(verbosity=2)
