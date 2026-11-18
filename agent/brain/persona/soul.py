"""人格檔（soul）：**照抄現役橋的形狀**，只換誰把它貼上去。

現役橋的做法（`ba/memory/soul_persona.py: persona_prefix`）：
  · 共用核心 `SOUL.md`（日語，§1 就是語言硬規則）
  · 再加**這個角色自己的檔** `<id>.md`（owner 2026-10-02 分檔：切角色不會讀到別人的）
  · 包成 `[[soul:<id>]] … [[/soul]]`，由殻（現在是 runtime）貼在**每一輪用戶訊息的前面**
  · 表情包名表／端末記憶／關係向量接在後面（那些在本倉各有去處，不屬於人格檔）

本檔只負責「讀＋組」。要不要貼、貼在哪，是 `brain/messages.py` 的事。
檔案語言是日語——**這就是她講日語的原因**，不是靠一條中文寫的規則去要求她。
"""

from __future__ import annotations

import pathlib
import threading
from typing import Mapping

#: 人格檔在訊息裡的包法（與現役橋逐字相同）。
OPEN = "[[soul:{id}]]"
CLOSE = "[[/soul]]"


class SoulLibrary:
    """讀 `SOUL.md` 與 `<角色>.md`。缺檔一律回空字串，絕不讓人格壞掉拖垮一輪。"""

    def __init__(self, root: str | pathlib.Path, *, core_name: str = "SOUL.md") -> None:
        self.root = pathlib.Path(root)
        self.core_name = core_name
        self._lock = threading.RLock()
        self._cache: dict[str, str] = {}
        self.missing: list[str] = []

    # ── 讀 ────────────────────────────────────────────────────────────────
    def _read(self, name: str) -> str:
        if not name:
            return ""
        with self._lock:
            if name in self._cache:
                return self._cache[name]
        text = ""
        path = self.root / name
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            with self._lock:
                if name not in self.missing:
                    self.missing.append(name)
        with self._lock:
            self._cache[name] = text
        return text

    def core(self) -> str:
        """共用核心（日語人格 + 語言規則）。"""
        return self._read(self.core_name)

    def character(self, soul: str) -> str:
        """角色檔。`soul` 可以帶 `.md`，也可以只給角色 id。"""
        name = str(soul or "").strip()
        if not name:
            return ""
        if not name.endswith(".md"):
            name = f"{name}.md"
        return self._read(name)

    def block(self, soul: str) -> str:
        """`[[soul:<id>]] … [[/soul]]`；核心與角色檔都缺就回空字串（呼叫端自己退回舊路徑）。"""
        name = str(soul or "").strip()
        core = self.core()
        own = self.character(name)
        body = "\n\n".join(part for part in (core, own) if part)
        if not body:
            return ""
        key = pathlib.Path(name or "default").stem or "default"
        return f"{OPEN.format(id=key)}\n{body}\n{CLOSE}"

    # ── 觀測 ──────────────────────────────────────────────────────────────
    def health(self) -> Mapping[str, object]:
        return {"root": str(self.root), "core": bool(self.core()), "missing": list(self.missing)}


__all__ = ["CLOSE", "OPEN", "SoulLibrary"]
