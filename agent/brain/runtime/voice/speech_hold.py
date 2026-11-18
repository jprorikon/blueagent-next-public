"""通話裡「還沒講完」的那句先扣住：主人接著講 ⇒ 兩句併成一句再送，不各起一輪。

情境（App 單人通話，2026-10-30）：App 端的 VAD 一靜音就把整句 POST 上來。主人說
「えっと、明日の」停一口氣再接「天気どう？」，以前會變成兩輪——第一輪她搶答半句，
第二輪 App 先打斷再送。現在：

    A 到 ──begin──> 轉寫＋判斷（並行）──resolve──> 判成「沒講完」⇒ 扣住，最多等 `hold_s`
    B 到 ──begin──> 轉寫 ──resolve──> 先等前面還在轉寫的句子 ⇒ 把扣住的 A 收進來（A 標成 merged）
    A 醒來發現被併走 ⇒ 不起回合；B 帶著「A＋B」起回合

規則：
  · 同一個 key（`<entity>:<call_id>`）內按到達順序；後到的會等前面還在轉寫的（最多 `settle_s`）。
  · 扣住期間如果已經有下一句到了（還在轉寫），A 就繼續等它來收，不自己先跑。
  · 判斷不出來（None）一律當講完；任何等待都有上限——寧可分兩輪，也不能吃掉一句話。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

#: 一句的狀態：轉寫中／扣住等人併／已被後一句收走／已放行
OPEN, HELD, MERGED, DONE = "open", "held", "merged", "done"


@dataclass
class _Entry:
    seq: int
    state: str = OPEN
    text: str = ""


@dataclass
class _Line:
    next_seq: int = 0
    entries: dict = field(default_factory=dict)


@dataclass(frozen=True)
class HoldTicket:
    key: str
    seq: int


@dataclass(frozen=True)
class HoldOutcome:
    #: 要送去起回合的文字（可能已經併了前面扣住的句子）
    text: str
    #: True ⇒ 這句被後一句收走了，**不要**起回合
    superseded: bool = False
    #: 併進來幾句前面的
    merged: int = 0
    #: 在這裡等了多久（毫秒；給 telemetry）
    waited_ms: int = 0


class SpeechHold:
    def __init__(self, *, hold_s: float = 1.5, settle_s: float = 4.0, clock=time.monotonic) -> None:
        self.hold_s = max(0.0, float(hold_s))
        self.settle_s = max(0.0, float(settle_s))
        self._clock = clock
        self._lines: dict[str, _Line] = {}
        self._cond = threading.Condition()

    def begin(self, key: str) -> HoldTicket:
        """一句話到了（還沒轉寫）：先排上號，後到的才知道要等它。"""
        with self._cond:
            line = self._lines.setdefault(str(key), _Line())
            seq = line.next_seq
            line.next_seq += 1
            line.entries[seq] = _Entry(seq)
            return HoldTicket(str(key), seq)

    def abandon(self, ticket: HoldTicket) -> None:
        """這句不送了（轉寫失敗、通話已掛）：放行，別讓後面的人白等。"""
        with self._cond:
            entry = self._entry(ticket)
            if entry is not None and entry.state in {OPEN, HELD}:
                entry.state = DONE
            self._cleanup(ticket.key)
            self._cond.notify_all()

    def resolve(self, ticket: HoldTicket, text: str, complete: bool | None) -> HoldOutcome:
        started = self._clock()
        with self._cond:
            line = self._lines.get(ticket.key)
            entry = self._entry(ticket)
            if line is None or entry is None:
                return HoldOutcome(text=str(text or ""))
            # ① 前面還在轉寫的先等它們落定（它們可能會被扣住，要收進來）
            self._wait(lambda: not any(row.state == OPEN for seq, row in line.entries.items()
                                       if seq < ticket.seq), started + self.settle_s)
            # ② 收走前面扣住的
            earlier = [row for seq, row in sorted(line.entries.items())
                       if seq < ticket.seq and row.state == HELD]
            for row in earlier:
                row.state = MERGED
            combined = "".join([row.text for row in earlier] + [str(text or "")])
            self._cond.notify_all()
            # ③ 自己沒講完 ⇒ 扣住，等後一句來收
            if complete is False and self.hold_s > 0:
                entry.state, entry.text = HELD, combined
                hold_until = self._clock() + self.hold_s
                settle_until = self._clock() + self.settle_s

                def released() -> bool:
                    if entry.state != HELD:
                        return True
                    now = self._clock()
                    later_open = any(row.state == OPEN for seq, row in line.entries.items()
                                     if seq > ticket.seq)
                    return now >= (settle_until if later_open else hold_until)

                self._wait(released, settle_until)
                if entry.state == MERGED:
                    self._cleanup(ticket.key)
                    return HoldOutcome(text=combined, superseded=True, merged=len(earlier),
                                       waited_ms=self._ms(started))
            entry.state = DONE
            self._cleanup(ticket.key)
            self._cond.notify_all()
            return HoldOutcome(text=combined, merged=len(earlier), waited_ms=self._ms(started))

    # ── 內部 ────────────────────────────────────────────────────────────────
    def _entry(self, ticket: HoldTicket):
        line = self._lines.get(ticket.key)
        return line.entries.get(ticket.seq) if line is not None else None

    def _wait(self, predicate, until: float) -> None:
        while not predicate():
            remaining = until - self._clock()
            if remaining <= 0:
                return
            self._cond.wait(timeout=min(remaining, 0.1))

    def _cleanup(self, key: str) -> None:
        line = self._lines.get(key)
        if line is None:
            return
        for seq in [seq for seq, row in line.entries.items() if row.state in {DONE, MERGED}]:
            line.entries.pop(seq, None)
        if not line.entries:
            self._lines.pop(key, None)

    def _ms(self, started: float) -> int:
        return round((self._clock() - started) * 1000)


__all__ = ["HoldOutcome", "HoldTicket", "SpeechHold"]
