"""她的関心（興趣播種）：把「她自己的興趣」與「對方的領域」分成兩桶。

為什麼要分（外部専案 Starlit 的 `interests.md` 讀法，owner 2026-11-03 要）：
  · **她自己的関心** ⇒ 自主任務／自主瀏覽的燃料：她會拿它去查、去玩、去寫筆記。
  · **對方的関心** ⇒ 只做接話材料。**不拿去查**——用別人的興趣去搜，只會搜出
    求職網與模型新聞這種垃圾頁（Starlit 實測結論，與我們 2026-10-27 的觀察一致）。

為什麼是 Markdown 而不是 JSON：人要能直接改（owner 的要求），而且判據只看**見出し**：
  · 見出しに「興味」を含み「相手」を含まない節 ⇒ 彼女自身の関心
  · 見出しに「相手」を含む節 ⇒ 相手の領域（接話用）
這條規則寫在檔案開頭，改檔案的人看得到，程式也只認這一條——不會有第二套語意。

缺檔 ⇒ 回空字串（零回歸：沒有這個檔的角色，行為跟以前完全一樣）。
"""

from __future__ import annotations

import pathlib
import threading

#: 檔名樣式（放在人格檔同一層，`agent/persona/<entity>.interests.md`）
SUFFIX = ".interests.md"

#: 餵進提示詞的上限：太長會把她自己的話擠掉（這一段只是燃料，不是主體）
MAX_CHARS = 700


def _headings(text: str) -> list[tuple[str, list[str]]]:
    """`[(見出し, [條目…])]`——只認 `#` 開頭的行，`-`／`・` 開頭的行算條目。"""
    sections: list[tuple[str, list[str]]] = []
    title, items = "", []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            if title or items:
                sections.append((title, items))
            title, items = line.lstrip("#").strip(), []
        elif line.startswith(("-", "・", "·", "*")):
            entry = line.lstrip("-・·* ").strip()
            if entry:
                items.append(entry)
    if title or items:
        sections.append((title, items))
    return sections


class InterestsLibrary:
    """讀 `<entity>.interests.md`：**只讀不寫**，照 mtime 快取，壞檔當沒有。"""

    def __init__(self, root, *, max_chars: int = MAX_CHARS) -> None:
        self.root = pathlib.Path(root) if root else None
        self.max_chars = max(0, int(max_chars))
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[float, tuple[list[str], list[str]]]] = {}

    # ── 讀 ────────────────────────────────────────────────────────────────
    def _load(self, entity: str) -> tuple[list[str], list[str]]:
        """`(自分の関心, 相手の領域)`；缺檔／壞檔 ⇒ 兩個空 list。"""
        name = str(entity or "").strip()
        if not name or self.root is None:
            return [], []
        path = self.root / f"{name}{SUFFIX}"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return [], []
        with self._lock:
            hit = self._cache.get(name)
            if hit is not None and hit[0] == mtime:
                return hit[1]
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return [], []
        own: list[str] = []
        other: list[str] = []
        for title, items in _headings(text):
            if "相手" in title:
                other.extend(items)
            elif "興味" in title or "関心" in title:
                own.extend(items)
        value = (own, other)
        with self._lock:
            self._cache[name] = (mtime, value)
        return value

    def own(self, entity: str) -> list[str]:
        return list(self._load(entity)[0])

    def other(self, entity: str) -> list[str]:
        return list(self._load(entity)[1])

    def block(self, entity: str) -> str:
        """給提示詞的那一小段；兩桶都空就回空字串（呼叫端不必判空檔）。"""
        own, other = self._load(entity)
        if not own and not other:
            return ""
        lines: list[str] = ["### 彼女の関心（自分から調べに行く側）"]
        lines += [f"・{item}" for item in own]
        if other:
            lines.append("### 相手の領域（自分からは調べない。話に乗るだけ）")
            lines += [f"・{item}" for item in other]
        return "\n".join(lines)[: self.max_chars]

    def health(self) -> dict:
        return {"root": str(self.root or ""), "cached": sorted(self._cache)}


__all__ = ["MAX_CHARS", "SUFFIX", "InterestsLibrary"]
