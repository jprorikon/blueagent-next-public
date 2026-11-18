"""每輪的「為什麼慢」取證（owner 2026-11-03：「先量為什麼慢」）。

一行看完：首字延遲、總時長、prompt 多大（以及**花在哪**）、快取命中多少、重試幾次。

為什麼要有這行：她 Chat 的 prompt 已到 11.5k tokens，但只靠 `prompt_tokens` 看不出
是「人格變胖／歷史變長／記憶段變多／工具定義太多」哪一個；有了分段字數，
下一次覺得慢就能直接對照，不必再猜。
"""

from __future__ import annotations

import json


def prompt_breakdown(stats: dict | None, *, tools_chars: int = 0, tools_count: int = 0) -> str:
    """把 `build_messages` 收集到的分段字數排成一句（大的排前面）。"""
    if not stats:
        return ""
    parts = [
        ("人格", int(stats.get("fixed", 0))),
        ("歷史", int(stats.get("history", 0))),
        ("本輪", int(stats.get("turn", 0))),
        ("這句", int(stats.get("current", 0))),
        ("工具", int(tools_chars)),
    ]
    parts = [(name, size) for name, size in parts if size]
    parts.sort(key=lambda item: -item[1])
    total = sum(size for _, size in parts)
    body = " ".join(f"{name}={size}" for name, size in parts)
    return f"{body} 合計≈{total}字/{tools_count}支工具"


def log_turn(entity: str, *, first_ms: int, total_ms: int, outcome: dict,
             stats: dict | None, attempts: int, body: dict) -> None:
    """印一行；`cached` 與 `prompt` 的比值說明這一輪是不是又整段重讀了。"""
    usage = (outcome or {}).get("usage") or {}
    finish = str((outcome or {}).get("finish_reason") or "")
    tools_chars, tools_count = tools_size((body or {}).get("tools"))
    prompt = int(usage.get("prompt_tokens") or 0)
    cached = int(((usage or {}).get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    ratio = f"{cached * 100 // prompt}%" if prompt else "-"
    detail = prompt_breakdown(stats, tools_chars=tools_chars, tools_count=tools_count)
    print(f"  turn[{entity}] 首字={first_ms}ms 總={total_ms}ms prompt={prompt} cached={cached}({ratio}) "
          f"重試={max(0, attempts - 1)} finish={finish or '-'} | {detail}", flush=True)


def tools_size(tools) -> tuple[int, int]:
    """工具定義的 (字數, 支數)——這段常被忽略，但它每輪都在。"""
    if not tools:
        return 0, 0
    try:
        return len(json.dumps(tools, ensure_ascii=False)), len(tools)
    except (TypeError, ValueError):
        return 0, len(tools)


__all__ = ["log_turn", "prompt_breakdown", "tools_size"]
