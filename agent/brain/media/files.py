"""她送出去的文件（不是圖）：`data/files/` 一顆檔案 ＋ 一行索引。

為什麼不塞進 `MediaStore`：那個倉只收 png/jpg/webp（6 MB 上限、給 VL 看的）。
「把觀測表當檔案送給主人」是另一件事——副檔名任意、上限也不同，混進去會讓
相册與視覺那條路吃到不該吃的東西（owner 2026-11-08「加上送文件到 discord 私聊的功能」）。

形狀刻意與 `MediaStore` 對齊（`{id, ext, bytes, at, entity, principal, name}`），
Discord 那邊才能用同一招「id → 本機路徑 → 附件」。
"""

from __future__ import annotations

import json
import pathlib
import re
import time
import uuid

#: 單檔上限（Discord 免費伺服器上傳上限 10 MB；留一點餘裕）
MAX_BYTES = 8 * 1024 * 1024
#: 檔名清洗：只留安全字元，避免 `..`／路徑分隔／控制字元跑到附件名
_BAD_NAME = re.compile(r"[\\/\x00-\x1f]")


def safe_name(name: str, fallback: str = "file.txt") -> str:
    clean = _BAD_NAME.sub("_", str(name or "").strip()).strip(". ")
    return clean[:120] or fallback


class FileStore:
    def __init__(self, root: pathlib.Path | str, *, max_bytes: int | None = None) -> None:
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index = self.root / "index.jsonl"
        self.max_bytes = int(MAX_BYTES if max_bytes is None else max_bytes)

    # ── 寫 ────────────────────────────────────────────────────────────────
    def save(self, data: bytes, *, name: str = "", principal: str = "",
             entity: str = "") -> dict:
        if not data:
            raise ValueError("empty")
        if len(data) > self.max_bytes:
            raise ValueError("too_large")
        clean = safe_name(name)
        ext = ("." + clean.rsplit(".", 1)[-1].lower()) if "." in clean else ""
        file_id = "f" + uuid.uuid4().hex[:16]
        path = self.root / (file_id + ext)
        path.write_bytes(data)
        row = {"id": file_id, "ext": ext, "bytes": len(data), "at": round(time.time(), 3),
               "entity": str(entity or ""), "principal": str(principal or ""), "name": clean}
        with self.index.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    # ── 讀 ────────────────────────────────────────────────────────────────
    def path_of(self, file_id: str) -> pathlib.Path | None:
        row = self.row_of(file_id)
        if row is None:
            return None
        path = self.root / (str(row.get("id")) + str(row.get("ext") or ""))
        return path if path.exists() else None

    def row_of(self, file_id: str) -> dict | None:
        for row in self._read():
            if str(row.get("id")) == str(file_id):
                return row
        return None

    def _read(self) -> list[dict]:
        if not self.index.exists():
            return []
        rows: list[dict] = []
        for line in self.index.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows


__all__ = ["FileStore", "MAX_BYTES", "safe_name"]
