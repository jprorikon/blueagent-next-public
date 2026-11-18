"""每一手的 prompt：把牌面、可做的動作、她的席位寫成她看得懂的一段。"""

from __future__ import annotations


from agent.adapter.trpg.decide import render_view


class SeatPrompts:
    """每一手給她看的 prompt（牌面＋規則＋她的席位）。"""

    # ── 內部 ────────────────────────────────────────────────────────────────
    def _prompt(self, view: dict, kick: dict, *, rejected: str = "") -> str:
        body = render_view(view, self.config.seat)
        # **沉浸**（owner 2026-10-23）：「她在遊戲裡提老師會出戲——桌上不一定有老師，
        # 而且會破壞 TRPG 的沉浸感」。所以桌上的發言一律是**角色**在說話，
        # 主人不在這張桌時，不准以任何形式呼叫他（オーナー／主人／owner 都不行）。
        owner_seat = self._owner_seat(view)
        if owner_seat:
            address = (f"ご主人さまもこの卓にいる（席は {owner_seat}）。彼に話しかけるときは"
                       "**卓上のそのキャラクター**として話すこと。「オーナー」とは呼ばない。")
        else:
            address = ("**ご主人さまはこの卓にいない**：この手では彼のことを出さない。"
                       "「オーナー／ご主人さま」とは呼ばない。卓の外の人にも話しかけない。")
        head = ("（あなたは今 TRPG を遊んでいる——**これはゲーム**：卓上のことはすべて虚構で、"
                "勝ち負けも怪我も死もゲームの中でしか起きない。"
                "あなたは今**卓上のそのキャラクター**で、そのキャラクターの口調と立場で話す。"
                "アシスタントとして話すのではない。"
                + address +
                "現実の相手への感情はゲームと切り離すこと：ゲームでうまくいかないことは、"
                "現実の人に腹を立てることではない。卓があなたを起こした。"
                "盤面を見て、この手を決めて。）")
        if kick.get("prompt"):
            # 桌端的話**接在後面**，不取代開頭（以前整段換掉 ⇒「這是遊戲」「主人不在桌上」的規矩全掉了）
            head += f"\n（卓から：{str(kick['prompt'])[:200]}）"
        tail = ("\n\n自分の口調で一言（日本語で）話して、ついでに何をするか示すこと。"
                "数値と結果は卓が計算する。あなたは宣言するだけでいい。")
        if rejected:
            tail = f"\n\n（前の手はルールに拒否された：{rejected}。できそうな別の手にすること。）"
        banned = [name for name, count in self.bans.items() if count >= 2]
        if banned:
            tail += "\n⛔ これらの行動はエンジンに二回以上拒否されている。この手では使ってはいけない：" + "、".join(banned)
        you = view.get("you") or {}
        hp, max_hp = you.get("hp"), you.get("max_hp")
        # 牌面沒給 HP（觀戰／替身）就不提醒——以前缺欄位算成 0/1 ⇒ 每手都喊「HP 很低」
        if hp is not None and max_hp and float(hp) / float(max_hp) <= 0.3:
            tail += "\n⚠️ HP がかなり低い：安全な場所へ下がるか回復を優先すること。無理に攻めない。"
        return f"{head}\n\n{body}{tail}"
