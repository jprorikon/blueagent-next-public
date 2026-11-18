"""串流的兩個守門員：**首幀預算**與**讀取超時放寬**（owner 2026-11-02 重排拆出來的）。

為什麼要獨立一支：`openai_compat.py` 有 600 行／單函式 150 行的棘輪（`tests/test_code_size.py`），
把這幾段留在 `_stream_turn` 裡會直接撞牆；而它們本身是純函式／純工具，搬出來剛好。

`deadline_ms` 的語意**只到第一個字**（`rules.DEADLINES` 的註解就是「首幀預算」）。以前它被
當成每一次 LLM 請求的 HTTP 超時 ⇒ 工具子回合的第 2、3 次請求也只有 8 秒，而那時 prompt 更長
（實測 2026-11-02 22:08：`subagents 5904ms → brain 38ms`、`first_token=null`、空回）。
"""

from __future__ import annotations

import json
import time


def first_token_budget(request) -> float:
    """這一輪「第一個正文 token」的預算（秒）；0＝不承諾。"""
    deadline = getattr(request, "deadline_ms", None)
    return float(deadline) / 1000.0 if deadline else 0.0


def first_token_late(first_seen: bool, budget_s: float, started: float,
                     now: float | None = None) -> bool:
    """第一個字還沒到、而且已經超過預算 ⇒ True。

    ⚠️ 只看**正文／工具呼叫**（呼叫端在收到時把 `first_seen` 設 True）。模型在 reasoning
    流裡慢慢想不算「卡住」——那是她在想，不是在掛。
    """
    if first_seen or budget_s <= 0:
        return False
    return (float(now) if now is not None else time.monotonic()) - started > budget_s


def relax_read_timeout(response, timeout: float) -> None:
    """把已建立連線的讀取超時放寬到「整次請求的上限」。

    等第一個字時用的是首幀預算（可能只有 3 秒），而 `socket` 的 timeout 是「每次讀之間的
    間隔」——不放寬的話，首幀過後的第 4 秒起，長回答就會被那條 3 秒切斷。
    拿不到底層 socket 就靜靜跳過（不同 Python／TLS 包裝層的屬性名不一樣）。
    """
    try:
        fp = getattr(response, "fp", None)
        raw = getattr(fp, "raw", None)
        sock = getattr(raw, "_sock", None) or getattr(fp, "_sock", None)
        if sock is None:
            sock = getattr(getattr(raw, "_fp", None), "raw", None)
        if sock is not None and hasattr(sock, "settimeout"):
            sock.settimeout(float(timeout))
    except Exception:                                     # noqa: BLE001 - 放寬失敗不影響讀取
        pass


def stream_failure(request, first_token_s: float, first_seen: bool, started: float,
                   exc: BaseException) -> tuple[str, str]:
    """斷線時要報哪一個碼：**還在等第一個字** ⇒ `first_token_timeout`；出過字 ⇒ `network_error`。

    分開的理由：前者是觀感問題（該切備援／先回一句兜底），後者是傳輸問題，兩者的處置不同。
    """
    spent = time.monotonic() - started
    first_miss = bool(first_token_s and not first_seen and spent >= min(first_token_s, 1.0))
    if first_miss:
        return ("first_token_timeout",
                f"第一個字 {int(first_token_s * 1000)} ms 內沒到（{type(exc).__name__}）")
    return ("network_error", f"{type(exc).__name__}: {str(exc)[:160]}")


def accumulate_tool_calls(pending: dict, items) -> None:
    """把串流裡的 `delta.tool_calls` 疊進 `pending[index]`（每個 index 一個呼叫）。"""
    for item in items or ():
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index") or 0)
        except (TypeError, ValueError):
            index = 0
        call = pending.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if item.get("id"):
            call["id"] = str(item["id"])
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        if function.get("name"):
            call["name"] = str(function["name"])
        if function.get("arguments") is not None:
            raw_args = function["arguments"]
            if isinstance(raw_args, (dict, list)):
                raw_args = json.dumps(raw_args, ensure_ascii=False)
            call["arguments"] += str(raw_args)


def finish_calls(pending: dict, swallowed, tool_names: dict, entity: str,
                 finish_reason=None) -> list[dict]:
    """把串流累積的呼叫收成清單（含文字形式的呼叫），解析不到時留取證。

    2026-11-04：她偶爾「finish_reason=tool_calls、但一個呼叫都沒解析出來」⇒ 管線當成
    她什麼都沒說（空白回覆）。這裡把原始累積印出來，才追得下去。
    """
    for extra in swallowed or ():              # 她寫成標籤的動作，照樣當呼叫
        pending[len(pending)] = {
            "id": "", "name": str((extra or {}).get("name") or ""),
            "arguments": json.dumps((extra or {}).get("arguments") or {}, ensure_ascii=False)}
    calls = [call for _, call in sorted(pending.items())
             if str(call.get("name") or "").strip()]
    if not calls and str(finish_reason or "") == "tool_calls":
        print(f"  brain[{entity}] 工具呼叫解析失敗："
              f"pending={json.dumps(pending, ensure_ascii=False)[:400]}", flush=True)
    for call in calls:
        call["name"] = (tool_names or {}).get(str(call["name"]), str(call["name"]))
    return calls


__all__ = ["accumulate_tool_calls", "finish_calls", "first_token_budget", "first_token_late",
           "relax_read_timeout", "stream_failure"]
