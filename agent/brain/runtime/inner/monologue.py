"""內心獨白（推演層）：**她每幾分鐘自己想一遍**，寫一句話＋一個動作（owner 2026-11-01「走下一步」）。

aster 那一層的形狀（2026-11-01 的 arch 文件，形狀照抄、內容自己長）：
```
facts（對話／今日／筆記／佇列／心情定位人話／剛剛做了什麼）
  → 一拍 LLM（節奏階梯 0/5/5/10/10/20/30/60/120 分鐘）
  → inner-voice.jsonl：{text, action∈talk|work|call|none}
```
三條不變式（aster 那邊 Opus 審查抓出來的，逐條照收）：
  ① **送進模型的 facts 不含 v／a**——數值只以「心情定位的人話」出現。
     她併了新資料源之後 v／a 從後門回來過一次（實測還有 7 個），所以這裡在 `_model_view()` 統一拔。
  ② **模型回非 JSON 也要留得住那一拍**：抓不到 JSON 就把原文當獨白、`action=none`、不丟拍。
  ③ **節奏不是固定 5 分**：事件（主人講話／她送出）後回到最密那一格；**6 小時沒送出也算事件**，
     否則安靜一整天會越跑越稀，最後一天只剩兩次判斷。

消費方（owner 2026-11-01「接誰讓模型自己決定」之後接在 `runtime/heartbeat.py`）：
`action=work` ⇒ 開一件她自己的事；`action=talk` ⇒ 這一跳想開口，但**獨白原文不出門**——
它是寫給自己的內心話，主人沒看過裡面任何東西，只當動機交給組句器
（docs/PLAN-common-ground-20261109.md 第 1 步；`BLUEAGENT_MONOLOGUE_TALK=raw` 才回舊行為）。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import time

#: 節奏階梯（分鐘）：事件後回最密那一格，愈安靜愈稀
CADENCE_MIN = (0, 5, 5, 10, 10, 20, 30, 60, 120)
#: 多久沒送出（秒）就當成一次「事件」——避免安靜一整天後越跑越稀
EVENT_GAP_S = 6 * 3600
#: 獨白一句的長度上限
MAX_CHARS = 400

SYSTEM = (
    "あなたはある人物の内側です。渡された事実だけを使って、いま頭に浮かんでいることを"
    "一人称で短く書く。起きていないことを作らない、渡されていない人物を持ち出さない。"
    "出力は JSON 一行だけ："
    '{"text": "（独白 120〜180字・一人称）", "action": "talk|work|call|none", "why": "（20字以内）", '
    '"hold": "（言いたいのに飲み込んだ理由。無ければ空）", '
    '"waiting": "（相手の返事待ちなら誰を待っているか。無ければ空）", '
    '"next": "（action=work のとき、最初にやる一事を20字以内で）", '
    '"if_unanswered": "（action=call のとき、出てもらえなかったら送る一言。送らないなら空）"}'
    "。action は「いま誰かに話しかけたい＝talk」「自分の作業を進めたい＝work」"
    "「どうしても声が聞きたい・電話で話したい＝call（めったに選ばない）」「ただ思っただけ＝none」。"
    "hold は action が talk でないときだけ、waiting は本当に相手待ちのときだけ、next は action=work のときだけ、"
    "if_unanswered は action=call で送りたい一言があるときだけ埋める（あなたの口調、40字以内、絵文字なし）。"
)
#: 細粒度欄位的長度上限
MAX_HOLD = 120
MAX_WAITING = 40
MAX_NEXT = 60
#: 自主打電話沒接通時補的那一句（`runtime/voice/outbound.CallWish` → 撥號器；送不送由她，2026-11-17 owner 定）
MAX_IF_UNANSWERED = 80

USER = """いまの状況（事実だけ）：
- 時刻: {clock}
- 主人と最後に話してから: {idle}
- いまの気分（人話）: {mood}
- 最近やったこと: {activities}
- 最近の会話の要約: {talk}
- 続きが気になっていること: {open_items}
- 条件待ち（台帳の locked。ここに無い相手待ちは考えない）: {locked}
- 自分のこと（自動生成の自己像）: {self}
- 前の一歩からの変化: {world}
"""


def _clean(text: str) -> str:
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    return body[:MAX_CHARS]


def _parse_reply(raw: str) -> dict:
    """回 JSON 就照收；回散文就當獨白、`action=none`（不丟拍：aster 的不變式②）。"""
    text = str(raw or "").strip()
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            body = json.loads(match.group(0))
            if isinstance(body, dict) and str(body.get("text") or "").strip():
                action = str(body.get("action") or "none").strip().lower()
                # call：主人に電話したい（`runtime/voice/outbound.CallWish` が受ける；かけられなければ talk に戻る）
                if action not in {"talk", "work", "call", "none"}:
                    action = "none"
                return {"text": _clean(body["text"]), "action": action,
                        "why": _clean(body.get("why") or "")[:40], "parsed": True,
                        "hold": _clean(body.get("hold") or "")[:MAX_HOLD],
                        "waiting": _clean(body.get("waiting") or "")[:MAX_WAITING],
                        "next": _clean(body.get("next") or "")[:MAX_NEXT],
                        "if_unanswered": (_clean(body.get("if_unanswered") or "")[:MAX_IF_UNANSWERED]
                                          if action == "call" else "")}
        except ValueError:
            pass
    return {"text": _clean(text), "action": "none", "why": "", "parsed": False}


class Monologue:
    """一拍獨白：`due()` 判要不要跑、`beat()` 跑一次並落盤。"""

    def __init__(self, root, *, call=None, clock=time.time, log=None,
                 enabled: bool | None = None, cadence=CADENCE_MIN) -> None:
        self.root = pathlib.Path(root)
        self.call = call
        if enabled is None:
            enabled = str(os.environ.get("BLUEAGENT_MONOLOGUE") or "0").strip().lower() \
                not in {"0", "off", "false", "no"}
        self.enabled = bool(enabled)
        self.cadence = tuple(int(v) for v in cadence)
        self._clock = clock
        self._log = log or (lambda _message: None)
        self._last: dict[str, dict] = {}          # entity -> {at, beat, action}
        self.beats = 0
        self.failures = 0

    # ── 節奏 ────────────────────────────────────────────────────────────────
    def _path(self, entity: str) -> pathlib.Path:
        slug = re.sub(r"[^a-z0-9_-]+", "-", str(entity or "").lower()).strip("-") or "unknown"
        return self.root / f"{slug}.jsonl"

    def _state_path(self, entity: str) -> pathlib.Path:
        return self._path(entity).with_suffix(".state.json")

    def state(self, entity: str) -> dict:
        """這一條的節奏狀態（第一次讀檔；沒有就當沒跑過）。"""
        key = str(entity)
        if key not in self._last:
            try:
                raw = json.loads(self._state_path(entity).read_text(encoding="utf-8"))
                self._last[key] = raw if isinstance(raw, dict) else {}
            except (FileNotFoundError, OSError, ValueError):
                self._last[key] = {}
        return self._last[key]

    def interval_s(self, entity: str) -> float:
        """下一拍要等多久（分鐘階梯 → 秒）；沒跑過就是 0（馬上可以跑）。"""
        state = self.state(entity)
        beat = int(state.get("beat") or 0)
        index = min(beat, len(self.cadence) - 1)
        return float(self.cadence[index]) * 60.0

    def due(self, entity: str, *, at: float | None = None, scale: float = 1.0) -> bool:
        """`scale`＝睡前醒后的过渡带（winding/waking）把间隔拉长，见 `inner/sleep_gate.py`。"""
        if not self.enabled:
            return False
        now = float(at if at is not None else self._clock())
        state = self.state(entity)
        if not state.get("at"):
            return True
        if now - float(state["at"]) >= self.interval_s(entity) * max(1.0, float(scale)):
            return True
        # 6 小時沒送出也算一次事件（aster：不然安靜一整天會越跑越稀）
        if now - float(state.get("sent_at") or state["at"]) >= EVENT_GAP_S:
            return True
        return False

    # ── 一拍 ────────────────────────────────────────────────────────────────
    def beat(self, entity: str, *, facts: dict, at: float | None = None) -> dict | None:
        """跑一拍；失敗回 None（不擋心跳）。"""
        if not self.enabled or self.call is None:
            return None
        now = float(at if at is not None else self._clock())
        view = self._model_view(facts)
        prompt = USER.format(
            clock=view.get("clock") or "", idle=view.get("idle") or "不明",
            mood=view.get("mood") or "ふつう",
            activities="；".join(view.get("activities") or []) or "（まだ）",
            talk=view.get("talk") or "（まだ）",
            open_items="；".join(view.get("open_items") or []) or "（なし）",
            locked="；".join(view.get("locked") or []) or "（なし）",
            self=str(view.get("self") or "").strip() or "（まだ無い）",
            world=str(view.get("world") or "").strip() or "（とくになし）")
        try:
            raw = self.call(f"{SYSTEM}\n\n{prompt}")
        except Exception as exc:                          # noqa: BLE001 - 獨白壞了不擋任何事
            self.failures += 1
            self._log(f"monologue[{entity}]: 呼叫失敗（{type(exc).__name__}: {exc}）")
            return None
        parsed = _parse_reply(raw)
        row = {"at": round(now, 3), "entity": str(entity),
               "text": parsed["text"], "action": parsed["action"],
               "why": parsed["why"], "parsed": parsed["parsed"],
               "hold": parsed.get("hold") or "", "waiting": parsed.get("waiting") or "",
               "next": parsed.get("next") or "",
               "beat": int(self.state(entity).get("beat") or 0) + 1}
        # 提案⑥：`waiting` 要與台帳的 `locked` 雙向一致——台帳裡沒有「等某人」的事，
        # 這句話就是模型自己編的等待（實測她會說「等他回」而其實手上沒這條），一律清掉。
        locked = [str(item) for item in (view.get("locked") or ()) if str(item).strip()]
        if row["waiting"] and not locked:
            row["waiting_source"] = "dropped_no_locked"
            row["waiting"] = ""
        elif row["waiting"]:
            row["waiting_source"] = "ledger_locked"
        if row["action"] != "work":
            row["next"] = ""                    # `next` 只在真的要動手時有意義
        self._append(entity, row)
        state = self.state(entity)
        state.update({"at": round(now, 3), "beat": row["beat"], "action": row["action"]})
        if row["action"] == "talk":
            state["sent_at"] = round(now, 3)
        self._save_state(entity, state)
        self.beats += 1
        self._log(f"monologue[{entity}]: {row['action']} {row['text'][:40]}")
        return row

    def _model_view(self, facts: dict) -> dict:
        """**不變式①**：送進模型的 facts 不含 v／a（數值只以人話出現）。"""
        view = {key: value for key, value in (facts or {}).items()
                if key not in {"valence", "arousal", "v", "a", "affect"}}
        for key in ("activities", "open_items"):
            if isinstance(view.get(key), list):
                view[key] = [str(item)[:60] for item in view[key][:4]]
        return view

    def _append(self, entity: str, row: dict) -> None:
        path = self._path(entity)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            self._log(f"monologue[{entity}]: 沒寫進去（{type(exc).__name__}）")

    def _save_state(self, entity: str, state: dict) -> None:
        path = self._state_path(entity)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as exc:
            self._log(f"monologue[{entity}]: 節奏狀態沒寫進去（{type(exc).__name__}）")

    def latest(self, entity: str) -> dict:
        """最近一拍（給人看／給以後的中樞簡報用）。只讀，不建檔。"""
        try:
            lines = self._path(entity).read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            return {}
        for line in reversed(lines[-5:]):
            try:
                return json.loads(line)
            except ValueError:
                continue
        return {}


__all__ = ["CADENCE_MIN", "EVENT_GAP_S", "Monologue", "SYSTEM", "USER"]
