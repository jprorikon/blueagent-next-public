"""匯報：她在桌上的近況（多久報一次、情緒門檻、報給誰）。"""

from __future__ import annotations

import time

from agent.adapter.trpg.decide import render_view


class SeatReporting:
    """匯報：每幾輪、情緒夠強才說一段給主人（預設只落本地）。"""

    # ── 匯報（以她自己的情緒為線索）──────────────────────────────────────────
    def _maybe_report(self, view: dict) -> None:
        """每 N 輪（且間隔夠）向她「報告場內狀況＋自己玩著的感想」。

        owner 2026-10-22：「她可以以她自己情绪为线索，报告一下场内的状况和她自己玩着的感想」。
        情緒不是另外編的：**走她自己的回合**（`speak` → 同一條 persona／心情／記憶），
        所以她當下的 mood 會直接決定她注意什麼、怎麼說；這裡只負責「什麼時候講」與「貼去哪」。
        """
        if self.report is None:
            return
        round_no = int(view.get("round_no") or (view.get("table") or {}).get("roundNo") or 0)
        if round_no and round_no == self._last_report_round:
            return
        if round_no and self.report_every_rounds > 1 and round_no % self.report_every_rounds:
            return
        if (time.time() - self._last_report_at) < self.report_min_gap_s:
            return
        # 情緒門檻：沒被牽動就不報（她的心情是「要不要講」的第一道閘，不是裝飾）
        if self.intensity is not None:
            try:
                self.last_intensity = float(self.intensity() or 0.0)
            except Exception:                              # noqa: BLE001 - 讀不到就當平盤
                self.last_intensity = 0.0
            if self.last_intensity < self.min_intensity:
                self._log(f"trpg: 第 {round_no} 輪情緒 {self.last_intensity:.2f} "
                          f"< 門檻 {self.min_intensity:.2f} ⇒ 不報")
                return
        prompt = self._report_prompt(view)
        try:
            text = str(self.speak(prompt, purpose="trpg-report") or "").strip()
        except Exception as exc:                            # noqa: BLE001 - 匯報壞了不影響打牌
            self._log(f"trpg: 匯報生成失敗（{type(exc).__name__}: {exc}）")
            return
        if not text:
            return
        try:
            if self.report(text, {"round": round_no, "seat": self.config.seat,
                                  "table": self.config.table_id}):
                self.reports += 1
                self._last_report_at = time.time()
                self._last_report_round = round_no
                self._log(f"trpg: 已匯報第 {round_no} 輪")
        except Exception as exc:                            # noqa: BLE001
            self._log(f"trpg: 匯報送出失敗（{type(exc).__name__}: {exc}）")

    def _owner_seat(self, view: dict) -> str:
        """主人在不在這張桌上？回他的席位（不在就空字）。

        判斷只看**席位名**（`seats`／`party` 裡的 name）：他的席位名通常就是「オーナー」或他自己的
        名字。找不到就當不在——寧可她少叫一次，也不要對著空氣喊人。
        """
        names = {token.strip().lower() for token in
                 str(getattr(self.config, "owner_names", "") or "オーナー,主人,オーナー,owner").split(",")
                 if token.strip()}
        rows: list[dict] = []
        for key in ("seats", "seat_list"):
            value = view.get(key)
            if isinstance(value, list):
                rows.extend([row for row in value if isinstance(row, dict)])
        for row in (view.get("board") or {}).get("party") or []:
            if isinstance(row, dict):
                rows.append(row)
        for row in rows:
            name = str(row.get("name") or "").strip().lower()
            if name and name in names:
                return str(row.get("seat") or row.get("id") or name)
        return ""

    def _report_prompt(self, view: dict) -> str:
        body = render_view(view, self.config.seat)
        return ("（**これは卓の外、卓上で話しているのではない**：今この手を打ち終えたところで、"
                "ご主人さまに今の状況をこっそり話したい。"
                "**自分の気持ち**を手がかりに："
                "今何が気になっているか、どこで緊張したか、どこで嬉しかったか。"
                "二、三文で、日本語で、彼に話しかけるように。"
                "箇条書きにしない、ルールを繰り返さない。)\n\n" + body)
