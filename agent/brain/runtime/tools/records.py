"""她自己的紀錄：筆記正文（`notes.read`）。

公共活動記錄（`activity.peek`）已在 2026-11-07 廢除（owner：「自動作從帖讀，公共活動記錄可以廢除」）：
對外唯一的公開面是公共區的帖（`board.feed`／`board.read`），活動日誌只留在內部（心跳、在場、
發布器）使用，不再有「查某人最近的公開記錄」這條工具面。
"""

from __future__ import annotations

from agent.brain.runtime.tools.base import ToolContext, ToolResult


class NotesReadTool:
    """`notes.read`：讀**她自己**的筆記（複習／自測用）。

    為什麼要它（owner 2026-10-24「複習自測」）：`activity.peek` 只看得到公開日誌的**一行摘要**，
    讀不到筆記正文 ⇒ 「複習」只能靠摘要唬過去。這裡直接讀 `data/notes/<她自己>/` 的段落。

    三條紀律：
      · 只讀自己的（`ctx.entity`），別人的筆記一律讀不到；
      · 標了 `〔private〕` 的段落預設跳過（那些是她的私記，不能流進公開產出）；
      · 只回最近的幾段，長度封頂，避免把整本日記倒進 prompt；
      · **只讀當天筆記檔 `YYYY-MM-DD.md`**，而且是最近 `days` 天的（2026-10-25 review）。
        同一個資料夾還有日記 `reflection-<日期>.md`：它按檔名排在筆記後面，以前「最後 N 個檔」
        取到的全是日記（真正的筆記一條都讀不到），日記裡的 `## ` 段落還會被當成筆記讀出來
        ⇒ 主人房間的日記內容流進公開產出。
    """

    name = "notes.read"
    #: 按需解鎖（技能 `notes`）；自主工作那條有自己的白名單，不受影響
    on_demand = True
    schema = {
        "type": "function",
        "function": {
            "name": "notes_read",
            "description": ("自分が最近書いたノートの本文を読む（復習・自習・古い材料を探すとき）。"
                            "読めるのは自分のノートだけ。private にした私的な段落は返らない。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "何日さかのぼるか（1〜14、既定 3）"},
                    "limit": {"type": "integer", "description": "最大段数（1〜24、既定 6）"},
                },
                "required": [],
            },
        },
    }

    def __init__(self, notes_root, *, clock=None) -> None:
        import pathlib
        import time
        self.root = pathlib.Path(notes_root)
        self._clock = clock or time.time

    def _dated_files(self, folder, days: int) -> list:
        """最近 `days` 天（含今天）的當天筆記檔，按日期排。日記等其他檔一律不算。"""
        import datetime
        today = datetime.date.fromtimestamp(float(self._clock()))
        earliest = today - datetime.timedelta(days=max(1, int(days)) - 1)
        dated = []
        for path in folder.glob("*.md"):
            try:
                day = datetime.date.fromisoformat(path.stem)   # `reflection-…` 會在這裡被擋掉
            except ValueError:
                continue
            if earliest <= day <= today:
                dated.append((day, path))
        return [path for _, path in sorted(dated)]

    def _sections(self, entity: str, days: int) -> list[str]:
        folder = self.root / str(entity)
        if not folder.is_dir():
            return []
        files = self._dated_files(folder, days)
        out: list[str] = []
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for block in text.split("\n## ")[1:]:
                # 私記不回流。標記可能在標頭，也可能在下一行（兩種寫法都認）
                head = "\n".join(block.split("\n")[:2])
                if "〔private〕" in head:
                    continue
                body = block.strip()
                if body:
                    out.append(f"（{path.stem}）{body}")
        return out

    def run(self, ctx, arguments: dict) -> "ToolResult":
        entity = str(getattr(ctx, "entity", "") or "").strip()
        if not entity:
            return ToolResult(ok=False, error="entity required")
        args = dict(arguments or {})
        try:
            days = max(1, min(14, int(args.get("days") or 3)))
        except (TypeError, ValueError):
            days = 3
        try:
            limit = max(1, min(24, int(args.get("limit") or 6)))
        except (TypeError, ValueError):
            limit = 6
        sections = self._sections(entity, days)[-limit:]
        if not sections:
            return ToolResult(ok=True, data={"entity": entity, "sections": [],
                                             "count": 0,
                                             "summary": f"{entity} はここ数日、読めるノートの段落がない。"})
        joined = "\n\n".join(sections)[:1600]
        return ToolResult(ok=True, data={"entity": entity, "sections": sections,
                                         "count": len(sections),
                                         "summary": f"{entity} の最近の {len(sections)} 段落：\n{joined}"})


__all__ = ["NotesReadTool"]
