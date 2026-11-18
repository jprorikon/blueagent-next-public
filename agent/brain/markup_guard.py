"""文字形式的工具呼叫攔截器（owner 2026-11-03 實機 bug）。

**現象**：主人在 App 問ノリ一句話，她回的不是話，而是一段
`<tool_call><function=motion><parameter=name>nod</parameter></tool_call>` ——
模型把「工具呼叫」用**文字**吐在正文裡（而不是走 `delta.tool_calls`），
串流直接把它當成她的話送到端末。

**為什麼會這樣**：Qwen 系（含現役 swift-1.5）在 function calling 不穩時會退回
XML 式記法。這不是她的錯，是鏈路少一道門。

**這支做什麼**：在串流路上擋住 `<tool_call>…</tool_call>`／`<function=…>`／
`<|tool_call|>` 這類記法——
  · 整段只有記法 ⇒ 當成一次真正的工具呼叫交回去（呼叫端照常執行），正文視為空；
  · 記法混在正文中間 ⇒ 只把記法那段吃掉，剩下的照常說出來；
  · 半截（串流切斷、沒有收尾標籤）⇒ 一樣吃掉，不讓殘骸流到端末。
"""

from __future__ import annotations

import re

#: 開頭長這樣就當「可能是文字形式的工具呼叫」，先壓住不發
_OPEN = re.compile(r"<\s*(tool_call|toolcall|\|tool_call\|)", re.IGNORECASE)
#: 收尾（沒有也要能吃）
_CLOSE = re.compile(r"</\s*(tool_call|toolcall|\|tool_call\|)\s*>", re.IGNORECASE)
_FUNC = re.compile(r"<\s*function\s*=\s*([A-Za-z0-9_.\-]+)\s*>", re.IGNORECASE)
_PARAM = re.compile(r"<\s*parameter\s*=\s*([A-Za-z0-9_.\-]+)\s*>(.*?)</\s*parameter\s*>",
                    re.IGNORECASE | re.DOTALL)
#: 沒有 `<tool_call>` 外殼、但直接寫了 `<function=…>` 的情況
_LOOSE_FUNC = re.compile(r"<\s*function\s*=\s*[A-Za-z0-9_.\-]+\s*>", re.IGNORECASE)

#: 壓住多少字還沒有結論就放棄壓制（避免把正常回話卡在緩衝裡）
HOLD_LIMIT = 400


def looks_like_markup(text: str) -> bool:
    """這段字看起來是不是工具呼叫記法（用來決定「要不要先壓住不發」）。"""
    head = (text or "").lstrip()[:40]
    if not head:
        return False
    if _OPEN.match(head) or _LOOSE_FUNC.match(head):
        return True
    # `<` 開頭、還沒出現 `>` ⇒ 可能是標籤還沒收完，先壓住
    return head.startswith("<") and ">" not in head


def parse_markup(text: str) -> dict | None:
    """把 `<tool_call><function=f><parameter=k>v</parameter></function></tool_call>` 解析成呼叫。

    回 `{"name": ..., "arguments": {...}}`；不是這種東西就回 None。
    """
    blob = text or ""
    name = _FUNC.search(blob)
    if not name:
        return None
    args: dict[str, str] = {}
    for key, value in _PARAM.findall(blob):
        args[key] = value.strip()
    return {"name": name.group(1), "arguments": args}


def strip_markup(text: str) -> str:
    """把記法整段吃掉，只留人話（含半截的殘骸）。"""
    blob = text or ""
    blob = re.sub(r"<\s*tool_call\s*>.*?(</\s*tool_call\s*>|$)", "", blob,
                  flags=re.IGNORECASE | re.DOTALL)
    blob = re.sub(r"<\s*\|tool_call\|\s*>.*?(</\s*\|tool_call\|\s*>|$)", "", blob,
                  flags=re.IGNORECASE | re.DOTALL)
    blob = re.sub(r"<\s*function\s*=\s*[A-Za-z0-9_.\-]+\s*>.*?(</\s*function\s*>|$)", "",
                  blob, flags=re.IGNORECASE | re.DOTALL)
    blob = re.sub(r"<\s*parameter\s*=\s*[A-Za-z0-9_.\-]+\s*>.*?(</\s*parameter\s*>|$)", "",
                  blob, flags=re.IGNORECASE | re.DOTALL)
    blob = re.sub(r"<\|[^|>]{1,40}\|>", "", blob)          # 其他特殊 token
    # 落單的收尾／殘骸（記法被串流切成兩段時，尾巴會自己跑出來）
    blob = re.sub(r"</?\s*(tool_call|toolcall|\|tool_call\||function|parameter)\s*>", "",
                  blob, flags=re.IGNORECASE)
    blob = re.sub(r"<\s*parameter\s*=\s*[A-Za-z0-9_.\-]+\s*>", "", blob, flags=re.IGNORECASE)
    return blob.strip()


class MarkupGuard:
    """串流路上的一段門：先壓住可能的記法，確認不是記法再放行。"""

    def __init__(self) -> None:
        self._hold = ""            # 壓住還沒判定的尾巴
        self.swallowed: list[dict] = []   # 抓到的工具呼叫

    def feed(self, piece: str) -> str:
        """吃一段串流文字，回「現在可以安全說出去的字」。"""
        if not piece:
            return ""
        self._hold += piece
        blob = self._hold
        # ① 整段都在記法裡（有開頭沒收尾）⇒ 繼續壓著
        if _OPEN.match(blob.lstrip()) or _LOOSE_FUNC.match(blob.lstrip()):
            if not (_CLOSE.search(blob) or "</function>" in blob.lower()):
                if len(blob) < HOLD_LIMIT:
                    return ""
            call = parse_markup(blob)
            if call:
                self.swallowed.append(call)
            leftover = strip_markup(blob)
            self._hold = ""
            return leftover
        # ② 可能只是標籤還沒收完（例如 "<" 或 "<funct"）
        stripped = blob.lstrip()
        if stripped.startswith("<") and ">" not in stripped and len(blob) < 40:
            return ""
        self._hold = ""
        # ③ 正常文字：夾在裡面的記法吃掉，但呼叫本身要留下來
        embedded = parse_markup(blob)
        if embedded:
            self.swallowed.append(embedded)
        return strip_markup(blob)

    def flush(self) -> str:
        """串流結束：剩下的字照規矩處理（殘骸一律吃掉）。"""
        blob, self._hold = self._hold, ""
        if not blob:
            return ""
        if _OPEN.match(blob.lstrip()) or _LOOSE_FUNC.match(blob.lstrip()):
            call = parse_markup(blob)
            if call:
                self.swallowed.append(call)
        return strip_markup(blob)


__all__ = ["MarkupGuard", "looks_like_markup", "parse_markup", "strip_markup"]
