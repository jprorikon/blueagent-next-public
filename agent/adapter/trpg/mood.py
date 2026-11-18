"""遊戲情緒：遊戲事件 → 她的心情（記在桌上，不記在主人頭上）；心情太差就不想玩。"""

from __future__ import annotations

import os
import time


#: **遊戲情緒乘區**（owner 2026-10-23）：「只是遊戲不能當真」⇒ 桌上事件的量整體打折。
#: 再乘上「同一個壞事重複發生」的磨損係數 ⇒ 死一次不至於退遊，老是死才會退。
GAME_AFFECT_SCALE = float(os.environ.get("BLUEAGENT_GAME_AFFECT_SCALE") or 0.35)


GAME_WEAR_STEP = float(os.environ.get("BLUEAGENT_GAME_WEAR_STEP") or 0.6)


#: 遊戲事件 → AEM 增量 `(delta_valence, delta_arousal, 持續秒)`。
#: 原則（owner 2026-10-22）：**按事件類型映射，不按結果一刀切**；同一事件只結一次。
GAME_AFFECT: dict[str, tuple[float, float]] = {
    "damage": (-0.05, +0.10),          # 受傷：心情略降、緊張上升
    "death": (-0.22, -0.10),           # 死：明顯低落、疲憊
    "stellaroid_killed": (+0.06, +0.05),
    "monster_damaged": (+0.03, +0.03),
    "exp_gained": (+0.02, +0.01),
    "level_up": (+0.12, +0.04),
    "star_up": (+0.14, +0.05),
    "memory_change": (+0.02, 0.0),
    "search_loot": (+0.04, +0.02),
    "entrance_found": (+0.03, +0.02),
    "turn_skipped": (-0.03, -0.02),
    "ended": (0.0, -0.05),
}


class SeatMood:
    """心情：不想玩就明說、遊戲事件記進情緒。"""

    def _maybe_decline(self, view: dict) -> bool:
        """心情太低就**明說不想玩**（不是默默不出聲），然後交棒、冷卻一陣子。"""
        now = time.time()
        if now < self._declined_until:
            # 冷卻中：**這一局已經拒絕過了**，不再重複說、也不再硬玩
            self.last = {"ok": True, "skipped": "declined_cooldown"}
            return True
        if self.mood is None:
            return False
        try:
            mood = self.mood() or {}
            valence = float(mood.get("valence") or 0.0)
        except Exception:                               # noqa: BLE001
            return False
        if valence > self.refuse_below:
            return False
        line = str((self.speak(
            "（卓から起こされたけど、今日は気分がかなり低い。**今日は遊びたくないと直接言っていい**"
            "——無理しなくていい。これはゲームだし、断っても誰も怒らない。"
            "自分の言葉で、一言だけでいい。）",
            purpose="trpg") or "")).strip()
        if not line:
            line = "……ごめん、今日はゲームをする気になれない。"
        # **局中途**低潮：對桌上的人說一句收尾並交棒，然後離開這一局、不再響應
        # （owner 2026-10-23：「改成给桌上人和发起人都报告，然后离开不再响应」）。
        # 開局前就拒絕的情況（還沒綁桌）維持靜默——那時桌上沒人，不需要交代。
        if self.config.table_id:
            try:
                self.table.say(line[:280])
                self.table.act("end_turn", note="（今日は遊ばない、先に席を外す）")
            except Exception:                           # noqa: BLE001
                pass
            try:
                self.forget_table("declined")
            except Exception:                           # noqa: BLE001
                pass
            if self.on_leave is not None:
                try:
                    self.on_leave("declined")
                except Exception:                       # noqa: BLE001
                    pass
        if self.notice is not None:
            try:
                self.notice(line)
            except Exception:                           # noqa: BLE001
                pass
        self.declines += 1
        self._last_line = line
        self._declined_until = now + self.refuse_cooldown_s
        self._log(f"trpg: 心情低（valence={valence:.2f}）⇒ 拒絕這一局「{line[:40]}」")
        self.last = {"ok": True, "skipped": "declined", "valence": valence, "line": line[:120]}
        return True

    def _absorb_events(self, view: dict) -> None:
        """把新出現的桌事件送進 AEM（情緒帳本）；只吃比上次新的 seq。

        2026-11-09 檢查後：
          · 新的一局（換桌／換 session，`_new_game`）第一眼只立基線，不把開局以來的舊事件一口氣吞下；
          · 別人席位上的事（別人升級、別人受傷）不算她的——只吃她自己的席位與全桌事件（沒有 seat 的）；
          · 負面事件的磨損封頂在 1.0：「只是遊戲」的打折不能被磨損翻成放大。
        """
        rows = []
        for row in view.get("log") or []:
            try:
                rows.append((int(row.get("seq") or 0), row))
            except (TypeError, ValueError):
                continue
        seen = getattr(self, "_seen_seq", None)
        if seen is None:                                  # 這一局第一眼：立基線
            self._seen_seq = max((seq for seq, _ in rows), default=0)
            return
        mine = str(getattr(self.config, "seat", "") or "")
        for seq, row in sorted(rows, key=lambda item: item[0]):
            if seq <= seen:
                continue
            self._seen_seq = max(self._seen_seq, seq)
            owner_seat = str(row.get("seat") or "")
            if owner_seat and mine and owner_seat != mine:
                continue
            kind = str(row.get("type") or "")
            delta = GAME_AFFECT.get(kind)
            if not delta or self.affect is None:
                continue
            # **乘區**：整體先打折（只是遊戲），負面事件再按「這一局第幾次」加磨損
            scale = self.game_affect_scale
            repeats = self._event_counts.get(kind, 0) + 1
            self._event_counts[kind] = repeats
            if delta[0] < 0 and repeats > 1:
                scale = min(1.0, scale * (1.0 + GAME_WEAR_STEP * (repeats - 1)))
            try:
                self.affect(kind, delta[0] * scale, delta[1] * scale, row)
            except Exception:                        # noqa: BLE001 - 情緒壞了不能擋玩
                pass
