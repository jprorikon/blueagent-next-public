"""遊樂場：**她自己的** ＋ **大家共用的**（owner 2026-10-22）。

```
data/playground/
├── mira/        ← 只有ミラ能讀寫（別人看不到）
├── aster/            ← 只有アスター
├── nori/            ← 只有ノリ
└── shared/         ← 三個人都能讀寫（留言板、共同筆記、素材）
```

三條紀律：

  · **自己的**：`scope="own"` 一定解析到呼叫者的 entity，呼叫端**沒有辦法**指到別人的資料夾
    （不是靠提示詞約束，是路徑解析就沒有那個入口）。
  · **共用的**：`scope="shared"` 誰都能寫；寫入時在檔尾記一行「誰、什麼時候」，
    所以她不會把別人的東西當自己的。
  · **只做文字**：v1 只收 UTF-8 文字（`md`／`txt`／`json`），單檔上限 256 KB；
    要放圖／音之後再開二進位通道，不要現在就混進來（混了之後備份與審計都會變難）。
"""

from __future__ import annotations

import pathlib
import re
import threading
import time

#: 單檔上限（位元組）。超過就拒收——playground 不是倉庫。
MAX_BYTES = 256 * 1024
#: 一次列多少檔（避免她一眼掃上千個）。
MAX_LIST = 50
#: 允許的副檔名（純文字）。
ALLOWED_SUFFIX = {".md", ".txt", ".json", ".csv", ".log", ""}

_SAFE = re.compile(r"^[^/\\\x00]+$")


def _slug(value: str, fallback: str = "unknown") -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("-")
    return clean or fallback


class Playground:
    def __init__(self, root, *, clock=time.time, log=None) -> None:
        self.root = pathlib.Path(root)
        self._clock = clock
        self._lock = threading.RLock()
        self._log = log or (lambda _message: None)

    # ── 位置 ────────────────────────────────────────────────────────────────
    def own_dir(self, entity: str) -> pathlib.Path:
        return self.root / _slug(entity)

    def shared_dir(self) -> pathlib.Path:
        return self.root / "shared"

    def resolve(self, entity: str, scope: str = "own") -> pathlib.Path:
        """`scope="own"` ⇒ 呼叫者自己的資料夾；`shared` ⇒ 共用。**沒有第三種**。"""
        kind = str(scope or "own").strip().lower()
        path = self.shared_dir() if kind == "shared" else self.own_dir(entity)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _target(self, entity: str, name: str, scope: str) -> pathlib.Path:
        clean = str(name or "").strip()
        if not clean or not _SAFE.match(clean) or clean in {".", ".."}:
            raise ValueError("bad name")
        if pathlib.PurePosixPath(clean).suffix.lower() not in ALLOWED_SUFFIX:
            raise ValueError("only text files")
        folder = self.resolve(entity, scope)
        target = (folder / clean).resolve()
        if folder.resolve() not in target.parents and target.parent != folder.resolve():
            raise ValueError("escapes the folder")
        return target

    # ── 讀寫 ────────────────────────────────────────────────────────────────
    def write(self, entity: str, name: str, content: str, *, scope: str = "own") -> dict:
        body = str(content or "")
        data = body.encode("utf-8")
        if len(data) > MAX_BYTES:
            raise ValueError(f"too large ({len(data)} > {MAX_BYTES})")
        target = self._target(entity, name, scope)
        with self._lock:
            existed = target.is_file()
            if str(scope).lower() == "shared" and not body.rstrip().endswith(
                    f"（{_slug(entity)}）"):
                # 共用的東西要留名：誰寫的、什麼時候
                stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(self._clock()))
                body = body.rstrip() + f"\n\n—— {_slug(entity)} · {stamp}（{_slug(entity)}）\n"
                data = body.encode("utf-8")
            target.write_bytes(data)
        return {"ok": True, "scope": str(scope).lower(), "name": target.name,
                "bytes": len(data), "created": not existed, "path": str(target)}

    def read(self, entity: str, name: str, *, scope: str = "own") -> dict:
        target = self._target(entity, name, scope)
        if not target.is_file():
            return {"ok": False, "error": "not_found", "name": name}
        text = target.read_text(encoding="utf-8", errors="replace")
        return {"ok": True, "name": target.name, "scope": str(scope).lower(),
                "text": text[:MAX_BYTES], "bytes": target.stat().st_size}

    def list(self, entity: str, *, scope: str = "own") -> dict:
        folder = self.resolve(entity, scope)
        rows = sorted((path for path in folder.iterdir() if path.is_file()),
                      key=lambda path: path.stat().st_mtime, reverse=True)[:MAX_LIST]
        return {"ok": True, "scope": str(scope).lower(),
                "files": [{"name": path.name, "bytes": path.stat().st_size} for path in rows]}

    def snapshot(self, entities=()) -> dict:
        def inventory(folder: pathlib.Path) -> dict:
            if not folder.is_dir():
                return {"files": 0, "bytes": 0}
            files = [path for path in folder.iterdir() if path.is_file()]
            return {"files": len(files), "bytes": sum(path.stat().st_size for path in files)}

        rows = {str(name): inventory(self.own_dir(name)) for name in entities}
        return {"root": str(self.root), "own": rows, "shared": inventory(self.shared_dir())}


__all__ = ["ALLOWED_SUFFIX", "MAX_BYTES", "Playground"]
