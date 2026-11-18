"""群聊点名判定：主人这句话是不是在叫某个角色（PLAN-chat-list §3.3「點名優先」）。

REVIEW 2026-11-08 A4：以前是「叫名出现在句子任何位置」就算点名 ⇒
  · ASCII 的 id 会在英文单词里误中（`aster` ⊂ period／scenario）；
  · 第三人称提到她（「ミラが言ってた件」）也被当成在叫她，其他人整轮被排除。

现在只认两种叫法：
  ① `@名字`（句中任何位置）；
  ② 句首就是名字（前面可以有空白或开引号／括号）——「ミラ、それどう？」「Aster, look」。
ASCII 名字两侧都要是词边界；日文名后面接什么都可以（「ミラちゃん」也算）。
"""

from __future__ import annotations

import re

#: 句首可以先出现的符号（开引号、括号、空白）
_LEAD = " \t　「『（(\"'“‘【"


def _ascii_word(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char == "_")


def _bounded(text: str, start: int, end: int) -> bool:
    """`text[start:end]` 两侧不是 ASCII 单词字符（只对 ASCII 名字有意义）。"""
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return not (before and _ascii_word(before)) and not (after and _ascii_word(after))


def _how(text: str, name: str) -> str:
    """`at`＝`@名字`；`named`＝句首叫名；空字串＝没叫。"""
    ascii_name = name.isascii()
    for match in re.finditer(r"@\s*" + re.escape(name), text):
        if not ascii_name or _bounded(text, match.end() - len(name), match.end()):
            return "at"
    head = text.lstrip(_LEAD)
    if head.startswith(name) and (not ascii_name or _bounded(head, 0, len(name))):
        return "named"
    return ""


def mention_kinds(text: str, aliases: dict) -> dict:
    """entity → `at`／`named`（群聊自聊 M5：`@` 拿起率 0.95，叫名 0.70，见 GROUP-SELF-CHAT §3）。"""
    folded = str(text or "").strip().casefold()
    found: dict = {}
    if not folded:
        return found
    for entity, names in (aliases or {}).items():
        for name in names:
            key = str(name or "").strip().casefold()
            how = _how(folded, key) if key else ""
            if how == "at" or (how and entity not in found):
                found[entity] = how
    return found


def mentioned(text: str, aliases: dict) -> set:
    """回被叫到的角色 id 集合。`aliases`：entity → 她自己的叫名（id／显示名／nicknames）。"""
    return set(mention_kinds(text, aliases))


__all__ = ["mention_kinds", "mentioned"]
