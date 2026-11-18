#!/usr/bin/env python3
"""任務委任的分界：`mode=work` 只有主人能開；guest 一律降級成聊天。

判據：
  ① owner 帶 `mode=work` ⇒ 原樣通過，`[[mode:work]]` 進到模型看到的那一輪；
  ② owner 帶 `mode=chat` ⇒ **仍按 work 執行**，note `owner_is_work`（2026-10-23 定）；
  ③ guest 帶 `mode=work` ⇒ 降級 `chat` + note `work_requires_owner`（不炸對話、也不偷偷執行）；
  ④ 未知 mode ⇒ 400；
  ⑤ 人格檔（SOUL.md）§5 寫明「委任只有オーナー」、§8 改成 entity×principal 且不再教她呼叫不存在的
     `remember` 道具——文字與程式要同一套規矩。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from demo_common import *  # noqa: F401,F403

from agent.brain.messages import build_messages
from agent.brain.domain.auth import AuthContext, Role
from agent.brain.persona.soul import SoulLibrary
from agent.brain.ports import BrainRequest
from agent.server import api as api_module

REPO = pathlib.Path(__file__).resolve().parents[2]
SOUL = REPO / "agent" / "persona" / "SOUL.md"

OWNER = AuthContext(principal="owner", role=Role.OWNER, capabilities=frozenset({"chat"}))
GUEST = AuthContext(principal="guest-x", role=Role.GUEST, capabilities=frozenset({"chat"}))


class TestPrepareSendGate(unittest.TestCase):
    def setUp(self):
        self.state = make_state()

    def body(self, **extra):
        return {"text": "帮我把这张图放大", "entity": "mira", **extra}

    def test_owner_can_open_work(self):
        status, data = api_module.prepare_send(self.state, self.body(mode="work"), auth=OWNER)
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "work")
        self.assertEqual(data["mode_note"], "")

    def test_owner_chat_is_still_work(self):
        """master 发起永远 work：传 chat 也只记 note，不降级。"""
        status, data = api_module.prepare_send(self.state, self.body(mode="chat"), auth=OWNER)
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "work")
        self.assertEqual(data["mode_note"], "owner_is_work")

    def test_guest_work_is_downgraded_not_rejected(self):
        status, data = api_module.prepare_send(self.state, self.body(mode="work"), auth=GUEST)
        self.assertEqual(status, 200, "降級而不是拒絕：對話要能繼續")
        self.assertEqual(data["mode"], "chat")
        self.assertEqual(data["mode_note"], "work_requires_owner")

    def test_guest_can_still_chat(self):
        status, data = api_module.prepare_send(self.state, self.body(), auth=GUEST)
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "chat")

    def test_unknown_mode_is_rejected(self):
        status, _ = api_module.prepare_send(self.state, self.body(mode="sudo"), auth=OWNER)
        self.assertEqual(status, 400)

    def test_guest_capability_cannot_be_forged_by_body(self):
        """body 裡自報 capabilities/mode 都沒用：能力位在 token 那一側。"""
        status, data = api_module.prepare_send(
            self.state, self.body(mode="work", capabilities=["work", "admin"]), auth=GUEST)
        self.assertEqual(status, 200)
        self.assertEqual(data["mode"], "chat")
        self.assertFalse(data["admin"])


class TestSendCarriesMode(unittest.TestCase):
    def test_work_mode_survives_into_result(self):
        state = make_state()
        result = state.send("帮我整理一下桌面", entity="mira", principal="owner",
                            mode="work")
        self.assertEqual(result["mode"], "work")

    def test_downgraded_mode_is_reported(self):
        state = make_state()
        result = state.send("帮我整理一下桌面", entity="mira", principal="owner",
                            mode="chat", mode_note="work_requires_owner")
        self.assertEqual(result["mode"], "chat")
        self.assertEqual(result["mode_note"], "work_requires_owner")


class TestModeTagInPrompt(unittest.TestCase):
    def request(self, mode: str) -> BrainRequest:
        return BrainRequest(thread_id="t", entity="mira", principal="owner",
                            text="帮我放大这张图", system="场景", mode=mode,
                            meta={"recall": [],
                                  "soul_block": "[[soul:mira]]\n人格\n[[/soul]]"})

    def test_work_tag_reaches_the_model(self):
        """mode tag 要跟著人格檔（2026-11-02：兩者都在固定前綴段，不在最後一條訊息）。"""
        messages = build_messages(self.request("work"))
        joined = "\n".join(str(item["content"]) for item in messages)
        self.assertIn("[[mode:work]]", joined)
        self.assertLess(joined.index("[[/soul]]"), joined.index("[[mode:work]]"),
                        "mode tag 要貼在人格檔之後")

    def test_chat_tag_by_default(self):
        joined = "\n".join(str(item["content"]) for item in build_messages(self.request("chat")))
        self.assertIn("[[mode:chat]]", joined)

    def test_no_soul_no_tag(self):
        """沒人格檔的 Demo 路徑不加 tag（避免動到原有 132 項的行為）。

        2026-11-02：本輪各段（這裡是 [[utterance]]）已併進最後一條用戶訊息，
        所以只驗「整份 prompt 裡沒有 mode tag」與「這句話仍在最後一條裡」。
        """
        request = BrainRequest(thread_id="t", entity="mira", principal="owner",
                               text="你好", system="S", mode="work", meta={"recall": []})
        messages = build_messages(request)
        joined = "\n".join(str(item["content"]) for item in messages)
        self.assertNotIn("[[mode:", joined)
        self.assertIn("你好", str(messages[-1]["content"]))


class TestSoulWording(unittest.TestCase):
    def setUp(self):
        self.text = SOUL.read_text(encoding="utf-8")

    def test_section5_limits_delegation_to_owner(self):
        self.assertIn("委任できるのはオーナー（owner）だけ", self.text)
        self.assertIn("ゲストの依頼を勝手に委任しない", self.text)

    def test_mode_tag_does_not_decide_who_is_speaking(self):
        """2026-11-11：以前寫「chat モードで届くのはゲストだけ」——App／通話／TRPG 的主人回合都是 chat，
        於是主人在那幾條路上被自己的規則當成客人。現在「誰在說話」看先頭那一行，mode 只表示能不能動機器。"""
        self.assertNotIn("このモードで届くのはゲストだけ", self.text)
        self.assertNotIn("オーナーは chat モードを持たない", self.text)
        self.assertIn("先頭の一行（「いま話しているのは …」）で決まる", self.text)

    def test_section8_describes_the_new_memory_model(self):
        self.assertIn("役（entity）× 相手（principal）", self.text)
        self.assertIn("その場にいる人 ⊆ その記憶の相手範囲", self.text)
        # 舊模型的描述與不存在的道具都要清掉
        self.assertNotIn("「どの端末の app か」×「どの役か」", self.text)
        self.assertNotIn('remember(text="オーナーは猫が好き")', self.text)

    def test_loaded_block_carries_the_new_wording(self):
        block = SoulLibrary(REPO / "agent" / "persona").block("mira")
        self.assertIn("委任できるのはオーナー（owner）だけ", block)
        self.assertIn("役（entity）× 相手（principal）", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
