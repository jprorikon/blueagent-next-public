"""私聊也可以「先不回」：她睡著的時候，私聊先放著，醒來再讀（owner 2026-11-09）。

> 「私聊也可以已讀不回（比如睡覺狀態可以不立馬回什麼的）」

以前生物鐘只管她自己的節奏，聊天一律秒回（`circadian.py` 文件頭：「聊天不受影響」）。現在：

  · **睡著（`asleep`）時收到私聊** ⇒ 不開回合、不回；整則存起來（`app-adapter/<她>-dm-asleep.json`）。
  · **醒了**（生物鐘不再是 `asleep`，含 `waking`）⇒ 每個會話只開**一輪**：拿最後一則當這一句，
    之前那幾則連同送達時刻放進 `[[room]]`（「眠っていた間に届いていた…いま起きて読んだ」），
    她自己決定怎麼回（一次回、不是一則一則回）。
  · **叫醒**：同一個人在 15 分鐘內傳了 3 則 ⇒ 當作被叫醒，馬上讀（前面幾則一樣放進 `[[room]]`）。
  · 群聊不走這裡（群聊本來就有「拿起手機」的機率，睡著時被 @ 0.3）；通話另有一套
    （`runtime/inner/call_pickup.py`：睡著不接、10 分鐘內第二通被吵醒）。

`BLUEAGENT_DM_SLEEP_HOLD=0` 關；`BLUEAGENT_DM_WAKE_COUNT`（3）／`BLUEAGENT_DM_WAKE_WINDOW_S`（900）。
"""

from __future__ import annotations

import os
import time

SUFFIX = "dm-asleep"


def _on() -> bool:
    return str(os.environ.get("BLUEAGENT_DM_SLEEP_HOLD") or "1").strip().lower() \
        not in {"0", "off", "false", "no"}


def _int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name) or default))
    except ValueError:
        return default


def _clock_of(adapter):
    table = getattr(getattr(adapter, "group_attention", None), "circadian", None) or {}
    return table.get(str(adapter.entity)) if isinstance(table, dict) else None


def _asleep(adapter, now: float) -> bool:
    clock = _clock_of(adapter)
    try:
        return bool(clock is not None and clock.asleep(float(now)))
    except Exception:                                   # noqa: BLE001 - 生物鐘壞了就當醒著
        return False


def _load(adapter) -> list[dict]:
    rows = (adapter.sidecar.read(SUFFIX) or {}).get("items")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _save(adapter, items: list[dict]) -> None:
    adapter.sidecar.write(SUFFIX, {"items": items[-200:]})


def _line(row: dict, names) -> str:
    at = time.strftime("%H:%M", time.localtime(float(row.get("at") or 0.0)))
    who = str(row.get("author") or "")
    name = "オーナー" if row.get("author_kind") == "owner" else names.get(who, who)
    return f"・{at} {name}「{str(row.get('text') or '')[:120]}」"


def note_for(held: list[dict], latest_at: float, names=None) -> str:
    """醒來那一輪的 `[[room]]`：睡著時先到的那幾則（帶時刻），以及最後這一則是幾點到的。"""
    names = dict(names or {})
    lines = ["（眠っていた間に届いていたメッセージ。いま起きて読んだ。返事はまとめて一度でいい）"]
    lines += [_line(row, names) for row in held]
    lines.append("最後の一言（いまの入力）が届いたのは "
                 + time.strftime("%H:%M", time.localtime(float(latest_at or 0.0))))
    return "\n".join(lines)


def extra(event: dict) -> dict:
    """`_handle` 把它併進回合參數：醒來那一輪才有 `room_note`。"""
    note = event.get("_slept")
    return {"room_note": str(note)} if note else {}


def hold(adapter, event: dict, conversation_id: str, message: dict, author: dict, text: str) -> bool:
    """睡著就先放著（回 True＝這一則先不處理）。被叫醒 ⇒ 回 False，並把先到的幾則塞進 `event["_slept"]`。"""
    if event.get("_slept") is not None or not _on():
        return False
    now = float(adapter._clock())
    if not _asleep(adapter, now):
        return False
    items = _load(adapter)
    row = {"conversation_id": str(conversation_id), "at": float(message.get("created_at") or now),
           "author": str(author.get("id") or ""), "author_kind": str(author.get("kind") or ""),
           "text": str(text or "（画像）"), "event": event}
    window = float(_int("BLUEAGENT_DM_WAKE_WINDOW_S", 900))
    same = [item for item in items if item["conversation_id"] == row["conversation_id"]
            and item["author"] == row["author"] and now - float(item.get("at") or 0.0) <= window]
    if len(same) + 1 >= _int("BLUEAGENT_DM_WAKE_COUNT", 3):
        earlier = [item for item in items if item["conversation_id"] == row["conversation_id"]]
        _save(adapter, [item for item in items if item["conversation_id"] != row["conversation_id"]])
        event["_slept"] = note_for(earlier, row["at"], getattr(adapter, "names", {})) \
            .replace("いま起きて読んだ", "何度も呼ばれて目が覚めた")
        adapter.log(f"app adapter[{adapter.entity}]: 睡著時被連傳 {len(earlier) + 1} 則 ⇒ 醒來讀")
        return False
    items.append(row)
    _save(adapter, items)
    adapter.log(f"app adapter[{adapter.entity}]: 睡著 ⇒ 私聊先放著（{len(items)} 則待讀）")
    return True


def tick(adapter, now: float) -> int:
    """醒了 ⇒ 每個會話開一輪（最後一則當這一句，先到的放進 `[[room]]`）。回開了幾輪。"""
    items = _load(adapter)
    if not items or _asleep(adapter, now):
        return 0
    _save(adapter, [])
    opened = 0
    order: list[str] = []
    for row in items:
        if row["conversation_id"] not in order:
            order.append(row["conversation_id"])
    for conversation_id in order:
        rows = [row for row in items if row["conversation_id"] == conversation_id]
        latest = rows[-1]
        event = dict(latest.get("event") or {})
        event["_slept"] = note_for(rows[:-1], latest.get("at") or now, getattr(adapter, "names", {}))
        try:
            adapter._handle(event)
            opened += 1
        except Exception as exc:                        # noqa: BLE001 - 這一輪失敗 ⇒ 放回去，下次再讀
            _save(adapter, _load(adapter) + rows)
            adapter.log(f"app adapter[{adapter.entity}]: 醒來讀私聊失敗（{type(exc).__name__}）⇒ 下次再讀")
    if opened:
        adapter.log(f"app adapter[{adapter.entity}]: 醒了 ⇒ 讀了睡著時的私聊（{opened} 個會話）")
    return opened


__all__ = ["extra", "hold", "note_for", "tick"]
