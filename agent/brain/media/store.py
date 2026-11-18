"""媒體（圖片）存放：`data/media/` 一顆檔案 ＋ 一行索引。

為什麼要有索引而不是只看目錄：相册要按時間倒序、要帶「誰送的／哪一輪」，
檔名的 mtime 不夠用（複製、解壓會改掉）。索引就是 `index.jsonl`，一行一筆，
讀的時候容忍壞行（手機殼不該為了一行壞資料就整個相册打不開）。

形狀刻意壓到最小：`{id, ext, bytes, at, principal, entity, turn_id, name}`。
"""

from __future__ import annotations

import json
import pathlib
import time
import uuid

#: 只收這幾種（不猜、不做轉檔——轉檔在客戶端做，伺服器只管存）
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp"}
#: 單張上限（與舊橋 `VISION_MAX_MB` 同一個量級；超過就 413，不進 VL）
MAX_BYTES = 6 * 1024 * 1024


class MediaStore:
    def __init__(self, root: pathlib.Path | str):
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index = self.root / "index.jsonl"

    # ── 寫 ────────────────────────────────────────────────────────────────
    def save(self, data: bytes, *, ext: str = ".png", principal: str = "",
             entity: str = "", name: str = "") -> dict:
        ext = (ext or ".png").lower()
        if ext not in ALLOWED_EXT:
            raise ValueError("unsupported_type")
        if not data:
            raise ValueError("empty")
        if len(data) > MAX_BYTES:
            raise ValueError("too_large")
        media_id = "m" + uuid.uuid4().hex[:16]
        path = self.root / (media_id + ext)
        path.write_bytes(data)
        row = {"id": media_id, "ext": ext, "bytes": len(data), "at": round(time.time(), 3),
               "principal": str(principal or ""), "entity": str(entity or ""),
               "turn_id": "", "name": str(name or "")}
        with self.index.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def attach_turn(self, media_id: str, turn_id: str) -> None:
        rows = self._read()
        changed = False
        for row in rows:
            if row.get("id") == media_id and not row.get("turn_id"):
                row["turn_id"] = str(turn_id)
                changed = True
        if changed:
            self._rewrite(rows)

    # ── 讀 ────────────────────────────────────────────────────────────────
    def list(self, *, offset: int = 0, limit: int = 30, principal: str = "") -> dict:
        rows = [r for r in self._read() if not principal or r.get("principal") == principal]
        rows.sort(key=lambda r: r.get("at", 0), reverse=True)
        window = rows[offset: offset + limit]
        return {"offset": offset, "limit": limit, "total": len(rows), "items": window}

    def path_of(self, media_id: str) -> pathlib.Path | None:
        for row in self._read():
            if row.get("id") == media_id:
                path = self.root / (media_id + row.get("ext", ".png"))
                return path if path.exists() else None
        return None

    def row_of(self, media_id: str) -> dict | None:
        for row in self._read():
            if row.get("id") == media_id:
                return row
        return None

    # ── 內部 ──────────────────────────────────────────────────────────────
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

    def _rewrite(self, rows: list[dict]) -> None:
        tmp = self.index.with_suffix(".tmp")
        tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                       encoding="utf-8")
        tmp.replace(self.index)
