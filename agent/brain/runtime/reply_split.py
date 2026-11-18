"""回覆的分段（server 側唯一一份）——owner 2026-10-23：「为什么不做在server侧」。

跟 TTS 的切句用**同一個句柄**：一條氣泡 = 最多 3 句；某條的最後一句要開始唸之前，
就把這一條當 `part` 幀推給端末 ⇒ 「唸的順序」與「氣泡出現的順序」天然對齊。

這是舊殼 `ReplySplit.kt` / C# `ReplySplit.cs` 的**逐條移植**：`fixtures/reply_split.json`
（Kotlin 側機器產生的真值）由 `tests/test_reply_split_parity.py` 逐欄對拍。
改這裡就要重跑那支；不要靠「看起來差不多」。
"""

from __future__ import annotations

import re

#: 一條氣泡最多幾句
SENTENCES_PER_BUBBLE = 3
#: 句尾（切句用，標點留在該句上）
SENTENCE_END = "。！？!?…．"
#: 句尾可能黏著的收尾符
TRAILING = "」』）)】］\"'”’"

#: 表情包記號（`:name`）：不念、也不算語音長度（這一層沒有名字表，只認形狀）。
#: 整條只有記號的氣泡必須算成 0 字。
_STICKER = re.compile(r":[a-z0-9_]{2,32}:?")
#: 作者自己的空行分段。JVM 的 `\s` 只有 ASCII——不要用 Python 的 `\s`（Unicode 級），
#: 否則 NBSP 之類的字元會被吃進空行判定（fixture `paragraph_break_with_unicode_space` 鎖住了）。
_BLANK_LINE = re.compile(r"\n[ \t\n\x0B\f\r]*\n")


def _is_whitespace(char: str) -> bool:
    """JVM `Char.isWhitespace()` 的等價物（`KotlinText.cs` 的 Python 版）。

    含 Zs／Zl／Zp 與 `\t \n \x0B \f \r`、U+001C–U+001F、NBSP／U+3000 等；
    **不含** U+0085（NEL）與 U+180E——Python 的 `str.isspace()` 會把它們當空白。
    """
    code = ord(char)
    if char in "\t\n\x0b\f\r\x1c\x1d\x1e\x1f":
        return True
    if code in (0x20, 0xA0, 0x1680, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000):
        return True
    return 0x2000 <= code <= 0x200A


def trim(text: str) -> str:
    """`String.trim()`：只裁頭尾的 JVM 空白。"""
    start, end = 0, len(text)
    while start < end and _is_whitespace(text[start]):
        start += 1
    while end > start and _is_whitespace(text[end - 1]):
        end -= 1
    return text[start:end]


def sentences(text: str) -> list[str]:
    """把一段文字切成句子（保留標點與收尾引號；換行也當句尾）。"""
    out: list[str] = []
    buf: list[str] = []
    body = str(text or "")
    index = 0
    while index < len(body):
        char = body[index]
        buf.append(char)
        index += 1
        if char == "\n":
            one = trim("".join(buf))
            if one:
                out.append(one)
            buf.clear()
            continue
        if char in SENTENCE_END:
            # 連續標點與其後的收尾符黏在同一句（「えっ！？」「…。」）
            while index < len(body) and (body[index] in SENTENCE_END or body[index] in TRAILING):
                buf.append(body[index])
                index += 1
            one = trim("".join(buf))
            if one:
                out.append(one)
            buf.clear()
    rest = trim("".join(buf))
    if rest:
        out.append(rest)
    return [one for one in out if one]


def groups(text: str, max_sentences: int = SENTENCES_PER_BUBBLE) -> list[str]:
    """切成氣泡：每條最多 `max_sentences` 句（條數隨長度長出來）。

    ① 先看作者自己的空行分段（分好的段不併掉）；
    ② 每段內部按句尾標點切句，每 `max_sentences` 句一條。
    """
    body = trim(str(text or ""))
    if not body:
        return []
    if max_sentences <= 1:
        return sentences(body) or [body]

    paragraphs = [trim(raw) for raw in _BLANK_LINE.split(body)]
    paragraphs = [one for one in paragraphs if one]
    if not paragraphs:
        return [body]

    output: list[str] = []
    step = int(max_sentences)
    for paragraph in paragraphs:
        lines = sentences(paragraph)
        if not lines:
            output.append(paragraph)
            continue
        for start in range(0, len(lines), step):
            output.append("".join(lines[start:start + step]))
    return [one for one in output if trim(one)]


def chars(text: str) -> int:
    """一段文字幾個字：不含空白（空白不出聲），也不含貼圖記號（貼圖不出聲）。

    長度用 **UTF-16 code unit** 算，跟 Kotlin／C# 的 `String.length` 一致：
    一個 emoji（代理對）算 2，不是 Python 的 1。語音長度估算靠這個數字。
    """
    stripped = _STICKER.sub("", str(text or ""))
    return sum(2 if ord(char) > 0xFFFF else 1
               for char in stripped if not _is_whitespace(char))


def speakable(text: str) -> bool:
    """這段文字念得出來嗎：只有貼圖記號 ⇒ 沒有東西可念。"""
    return chars(text) > 0


def bounds_ms(groups_list: list[str], ms_per_char: int) -> list[int]:
    """每條結束時的累計語音長度估算（毫秒）：字數 × `ms_per_char`。"""
    out: list[int] = []
    total = 0
    for one in groups_list:
        total += chars(one) * int(ms_per_char)
        out.append(total)
    return out


def ready_count(groups_list: list[str], *, final: bool) -> int:
    """現在可以推出去幾條：串流中「最後一條還在長」⇒ 只推到倒數第二條。"""
    if not groups_list:
        return 0
    return len(groups_list) if final else max(0, len(groups_list) - 1)


__all__ = ["SENTENCES_PER_BUBBLE", "bounds_ms", "chars", "groups", "ready_count",
           "sentences", "speakable", "trim"]
