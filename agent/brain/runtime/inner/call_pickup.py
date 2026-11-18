"""接不接電話：主人在 App 撥過來時，先由她決定要不要接（owner 2026-11-17）。

> 「App 的通話應該讓 Agent 決定是否接通，確定接通後才走語音通話路線。」

以前 `/v1/calls` 一開就是接通。現在 App Server 先問這裡（`app_voice` 的 `ring` 動作），
**接了**才掛上 `VoiceService`、進 REALTIME 回合；**不接**就回一個理由，App 顯示對應畫面。

判斷分兩層，整體有時間上限（不讓主人一直聽嘟嘟聲）：

1. **規則**（不叫模型，毫秒級）：
   · 她**正在別的通話裡**（Discord 語音房、另一端的 App 通話）⇒ 不接，`on_call`（話し中）。
   · 她**睡著**（生物鐘 `asleep`）⇒ 不接，`asleep`（響到沒人接）；**同一個人 10 分鐘內第二次打來**
     ⇒ 當作被電話吵醒、接起來（`woken`，第一句帶「被吵醒、還迷糊」的 `[[room]]`）。
     與私聊「連傳 3 則叫醒」（`adapter/app_dm_sleep.py`）同一個想法，電話比文字更吵，所以 2 次。
   · 沒有任何「在忙」的跡象 ⇒ 馬上接（`free`）。
2. **模型**（只在有「在忙」跡象時）：手上有進行中的活動、正在跑別的回合、睡前／剛醒——
   問一支短呼叫（`side.elicit`）「接不接、不接的話說一句什麼」，**最多等 `judge_timeout_s`（3 秒）**；
   逾時、壞 JSON、沒接模型 ⇒ 接（寧可接起來再說，不要因為判斷器慢就讓主人吃閉門羹）。

拒接的那一句（`line`）怎麼用由 `adapter/app_voice.py` 決定：顯示在通話畫面，並當主動私聊送出。

`BLUEAGENT_CALL_PICKUP=0` ⇒ 一律接（回到舊行為）；`BLUEAGENT_CALL_PICKUP_JUDGE=0` ⇒ 只用規則。
`BLUEAGENT_CALL_WAKE_COUNT`（2）／`BLUEAGENT_CALL_WAKE_WINDOW_S`（600）／`BLUEAGENT_CALL_JUDGE_TIMEOUT_S`（3）。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass

WOKEN_NOTE = "（さっきまで眠っていたが、電話の音で目が覚めて出た。まだ少し寝ぼけている）"
#: 模型沒給句子時的備用（拒接時一定要有一句，畫面和私聊才不會空著）
FALLBACK_LINE = "ごめん、いまちょっと出られない。あとで連絡するね"
LINE_MAX = 80


def _flag(name: str, default: str = "1") -> bool:
    return str(os.environ.get(name) or default).strip().lower() not in {"0", "off", "false", "no"}


def _number(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name) or default))
    except ValueError:
        return default


@dataclass
class Pickup:
    """一次響鈴的結果。`reason` 是給程式分支用的固定字（不是給人看的話）。"""

    answer: bool
    reason: str
    line: str = ""
    note: str = ""
    signals: tuple = ()
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return {"answer": self.answer, "reason": self.reason, "line": self.line,
                "signals": list(self.signals), "elapsed_ms": self.elapsed_ms}


class CallPickup:
    """`decide(app, entity, principal, call_id)` → `Pickup`。記住最近的響鈴（判斷「又打來了」）。"""

    def __init__(self, *, judge=None, clock=time.time, log=None) -> None:
        #: `judge(prompt) -> str`：短呼叫（`SideCalls.elicit`）；None＝只用規則
        self.judge = judge
        self._clock = clock
        self._log = log or (lambda _message: None)
        self._lock = threading.Lock()
        #: `(entity, principal) → [響鈴時刻…]`（只留窗口內的）
        self._rings: dict[tuple[str, str], list[float]] = {}
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="call-pickup")
        self.judge_timeout_s = _number("BLUEAGENT_CALL_JUDGE_TIMEOUT_S", 3.0)
        self.wake_window_s = _number("BLUEAGENT_CALL_WAKE_WINDOW_S", 600.0)
        self.wake_count = max(1, int(_number("BLUEAGENT_CALL_WAKE_COUNT", 2)))

    # ── 對外 ────────────────────────────────────────────────────────────────
    def decide(self, app, entity: str, principal: str, call_id: str = "") -> Pickup:
        started = time.monotonic()
        result = self._decide(app, str(entity), str(principal), str(call_id or ""))
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        self._log(f"call pickup: {entity} ← {principal} ⇒ "
                  f"{'接' if result.answer else '不接'}（{result.reason}；"
                  f"{','.join(result.signals) or '-'}；{result.elapsed_ms} ms）")
        return result

    def _decide(self, app, entity: str, principal: str, call_id: str) -> Pickup:
        if not _flag("BLUEAGENT_CALL_PICKUP"):
            return Pickup(True, "always")
        now = float(self._clock())
        rings = self._note_ring(entity, principal, now)
        if self._on_other_call(app, entity, call_id):
            return Pickup(False, "on_call")
        state = self._clock_state(app, entity, now)
        if state == "asleep":
            if rings >= self.wake_count:
                return Pickup(True, "woken", note=WOKEN_NOTE, signals=("asleep",))
            return Pickup(False, "asleep", signals=("asleep",))
        signals = self._busy_signals(app, entity, state)
        if not signals:
            return Pickup(True, "free")
        return self._ask(app, entity, principal, signals)

    # ── 規則 ────────────────────────────────────────────────────────────────
    def _note_ring(self, entity: str, principal: str, now: float) -> int:
        """記這一次響鈴，回窗口內（含這次）一共響了幾次。"""
        key = (entity, principal)
        with self._lock:
            rows = [at for at in self._rings.get(key, []) if now - at <= self.wake_window_s]
            rows.append(now)
            self._rings[key] = rows[-10:]
            return len(rows)

    @staticmethod
    def _on_other_call(app, entity: str, call_id: str) -> bool:
        voice = getattr(app, "voice", None)
        try:
            active = voice.active(entity) if voice is not None else None
        except Exception:                                   # noqa: BLE001 - 查不到當作沒有
            return False
        return active is not None and str(getattr(active, "id", "")) != call_id

    @staticmethod
    def _clock_state(app, entity: str, now: float) -> str:
        clocks = getattr(app, "circadian", None)
        clock = clocks.get(entity) if isinstance(clocks, dict) else None
        if clock is None:
            return "awake"
        try:
            return str(clock.state(now) or "awake")
        except Exception:                                   # noqa: BLE001 - 生物鐘壞了就當醒著
            return "awake"

    @staticmethod
    def _busy_signals(app, entity: str, state: str) -> tuple:
        """「她可能在忙」的跡象（給模型看的短標籤）。空＝閒著。"""
        signals: list[str] = []
        if state in {"winding", "waking"}:
            signals.append(state)
        lives = getattr(app, "lives", None)
        loop = lives.get(entity) if isinstance(lives, dict) else None
        try:
            activity = ((loop.snapshot() or {}).get("activity") or {}) if loop is not None else {}
        except Exception:                                   # noqa: BLE001
            activity = {}
        if str(activity.get("status") or "") == "running":
            signals.append("activity:" + str(activity.get("title") or "")[:40])
        scheduler = getattr(app, "scheduler", None)
        try:
            lanes = ((scheduler.snapshot() or {}).get("active_by_entity") or {}).get(entity) or {}
        except Exception:                                   # noqa: BLE001
            lanes = {}
        if any(count for lane, count in lanes.items() if str(lane).lower() != "realtime"):
            signals.append("mid_turn")
        return tuple(signals)

    # ── 模型 ────────────────────────────────────────────────────────────────
    def _ask(self, app, entity: str, principal: str, signals: tuple) -> Pickup:
        if self.judge is None or not _flag("BLUEAGENT_CALL_PICKUP_JUDGE"):
            return Pickup(True, "busy_but_answered", signals=signals)
        prompt = self.prompt(app, entity, principal, signals)
        future = self._pool.submit(self.judge, prompt)
        try:
            raw = future.result(timeout=self.judge_timeout_s)
        except FutureTimeout:
            future.cancel()
            return Pickup(True, "judge_timeout", signals=signals)
        except Exception:                                   # noqa: BLE001 - 判斷器壞了 ⇒ 接
            return Pickup(True, "judge_failed", signals=signals)
        verdict = parse_verdict(raw)
        if verdict is None:
            return Pickup(True, "judge_unparsable", signals=signals)
        answer, line = verdict
        if answer:
            return Pickup(True, "judged_yes", signals=signals)
        return Pickup(False, "declined", line=line or FALLBACK_LINE, signals=signals)

    @staticmethod
    def prompt(app, entity: str, principal: str, signals: tuple) -> str:
        entities = getattr(app, "entities", None)
        found = entities.get(entity) if entities is not None and hasattr(entities, "get") else None
        name = str(getattr(found, "name", "") or entity)
        mood = ""
        affect = getattr(app, "affect", None)
        if affect is not None:
            try:
                snap = affect.mood(entity) or {}
                mood = "、".join(f"{key}={snap[key]}" for key in ("label", "valence", "arousal")
                                if key in snap)
            except Exception:                               # noqa: BLE001
                mood = ""
        labels = {"winding": "もうすぐ寝る時間", "waking": "起きたばかり", "mid_turn": "別の会話に返事を書いている途中"}
        lines = []
        for signal in signals:
            if signal.startswith("activity:"):
                lines.append("いまやっていること：" + (signal.split(":", 1)[1] or "作業"))
            else:
                lines.append(labels.get(signal, signal))
        return (f"あなたは{name}。いま、オーナー（{principal}）から電話がかかってきた。\n"
                f"いまの状況：{'／'.join(lines)}\n"
                + (f"気分：{mood}\n" if mood else "")
                + "大抵は出る。手が離せない・本当に眠い・気分的に話せない時だけ出ない。\n"
                "出ない場合は、あとでオーナーに送る短い一言（あなたの口調、40字以内、絵文字なし）も書く。\n"
                '次のJSONだけを返す：{"answer": true} または {"answer": false, "line": "…"}')


def parse_verdict(raw) -> tuple[bool, str] | None:
    """模型回的字 → `(接不接, 一句)`；看不懂 ⇒ None。"""
    text = str(raw or "").strip()
    match = re.search(r"\{.*\}", text, re.S)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("answer"), bool):
        return None
    line = re.sub(r"\s+", " ", str(data.get("line") or "")).strip()[:LINE_MAX]
    return data["answer"], line


__all__ = ["CallPickup", "Pickup", "parse_verdict", "FALLBACK_LINE", "WOKEN_NOTE"]
