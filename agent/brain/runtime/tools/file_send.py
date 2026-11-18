"""`file.send`：把她自己的檔案當附件送給主人（owner 2026-11-08「加上送文件到 discord 私聊的功能」）。

由來：她 11-06 被要求「観測記録送ってくれる？」時，手上只有 `delegate`（只能出**圖**）
與貼圖——沒有「把一份 .md 交出去」的手段，於是那一輪回了空字，變成連五次催主人「次、オーナーの番」。
這支補的就是那個缺口：她能送的東西＝**自己的**筆記／作品／VM 裡的家目錄。

邊界（刻意收窄）：
  · 只讀 `data/notes/<角色>`、`data/works/<角色>` 兩個根底下（路徑穿越一律擋）；
  · 上限 8 MB（Discord 免費伺服器 10 MB，留餘裕），空檔不送；
  · 只在 Discord 渠道出現（App 那端還沒有檔案泡泡的 UI）；
  · 只回一個 `file` 幀——真正的上傳在 Discord transport 那側（它拿 id 反查本機檔案）。
"""

from __future__ import annotations

import os
import pathlib

from agent.brain.runtime.tools.base import ToolResult

#: 她可以送出哪些根目錄（相對 data/）：她自己的東西，不是整個家。
#: ⚠️ 不含 `vm/`——那底下放的是「VM 的連線設定」（`data/vm/<entity>.json`），
#: 不是她的家目錄；VM 裡的檔案要用 terminal 看，要送得先拉回來（還沒做）。
SEND_ROOTS = ("notes", "works")


class FileSendTool:
    name = "file.send"
    channels = ("discord",)
    schema = {
        "type": "function",
        "function": {
            "name": "file_send",
            "description": (
                "自分のファイルを添付で送る（Discord の今のチャンネル。私聊なら DM に届く）。"
                "**path を省略すると、送れるファイルの一覧が出る**——まずそれで名前を確かめる。"
                "送れるのは自分のノート（notes/…）と自分の作品（works/…）だけ。"
                "VM（terminal）の中のファイルはここからは送れない——terminal で探しても無い。"
                "例：path=\"works/2026-11-08-観測表.md\"。図や写真を送りたいときは委任（画像）を使う。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "notes/… か works/… から始まる相対パス（省略＝一覧）"},
                    "name": {"type": "string",
                             "description": "添付のファイル名（省略＝元のファイル名）"},
                },
                "required": [],
            },
        },
    }

    def __init__(self, files, *, base, max_mb: float | None = None) -> None:
        self.files = files
        self.base = pathlib.Path(base)
        if max_mb is None:
            max_mb = float(os.environ.get("BLUEAGENT_FILE_SEND_MAX_MB") or 0) or None
        if max_mb:
            self.files.max_bytes = int(float(max_mb) * 1024 * 1024)

    # ── 路徑 ────────────────────────────────────────────────────────────────
    def _roots(self, entity: str) -> list[pathlib.Path]:
        name = str(entity or "")
        return [(self.base / root / name) for root in SEND_ROOTS]

    def resolve(self, entity: str, raw: str) -> pathlib.Path | None:
        """把模型給的路徑對到「她自己的」某個檔案；對不到就 None（含路徑穿越）。"""
        text = str(raw or "").strip()
        if not text:
            return None
        roots = [path.resolve() for path in self._roots(entity)]
        candidates: list[pathlib.Path] = []
        given = pathlib.Path(text)
        if given.is_absolute():
            candidates.append(given)
        else:
            candidates.extend(root / text for root in roots)
            # `works/観測表.md` → `data/works/<entity>/観測表.md`（描述裡就是這樣寫的）
            for root in SEND_ROOTS:
                prefix = root + "/"
                if text.startswith(prefix):
                    candidates.append(self.base / root / str(entity or "") / text[len(prefix):])
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except (OSError, RuntimeError):
                continue
            if not any(resolved == root or root in resolved.parents for root in roots):
                continue                        # 不在她自己的三個根底下
            if resolved.is_file():
                return resolved
        # 名稱猜錯也不要叫她再跑一回合查（一回合 ≈25–30 秒，實測 2026-11-08）：
        # 用子字串對一次，**只有唯一命中才算**（多筆就回 None，讓她自己看一覧）。
        token = text.replace(" ", "")
        hits = [row for row in self.listing(entity, limit=200)
                if token and token in str(row["path"]).replace(" ", "")]
        if len(hits) == 1:
            return self.resolve(entity, hits[0]["path"])
        return None

    def listing(self, entity: str, *, limit: int = 20) -> list[dict]:
        """她送得出的檔案（三個根底下，新的在前）——沒有這個她只能瞎猜檔名。"""
        rows: list[dict] = []
        for root_name in SEND_ROOTS:
            root = self.base / root_name / str(entity or "")
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                rows.append({"path": f"{root_name}/{path.relative_to(root)}",
                             "bytes": stat.st_size, "at": round(stat.st_mtime, 1)})
        rows.sort(key=lambda row: row["at"], reverse=True)
        return rows[:max(1, int(limit))]

    # ── 執行 ────────────────────────────────────────────────────────────────
    def run(self, ctx, arguments: dict) -> ToolResult:
        raw = str((arguments or {}).get("path") or "").strip()
        if not raw:
            return ToolResult(ok=True, data={"items": self.listing(getattr(ctx, "entity", "")),
                                             "note": "path を付けて呼ぶと、そのファイルを添付で送る"})
        path = self.resolve(getattr(ctx, "entity", ""), raw)
        if path is not None and path.is_dir():
            return ToolResult(ok=True, data={"items": self.listing(getattr(ctx, "entity", ""))})
        if path is None:
            return ToolResult(ok=False, error="file_not_allowed",
                              data={"path": raw,
                                    "allowed": [f"{root}/<自分の役割>/…" for root in SEND_ROOTS]})
        try:
            data = path.read_bytes()
        except OSError as exc:
            return ToolResult(ok=False, error=f"read_failed: {type(exc).__name__}")
        name = str((arguments or {}).get("name") or "").strip() or path.name
        try:
            row = self.files.save(data, name=name, principal=str(getattr(ctx, "principal", "")),
                                  entity=str(getattr(ctx, "entity", "")))
        except ValueError as exc:
            return ToolResult(ok=False, error=str(exc) or "save_failed")
        frame = {"kind": "file",
                 "payload": {"id": row["id"], "name": row["name"], "bytes": row["bytes"]}}
        return ToolResult(ok=True, data={"id": row["id"], "name": row["name"],
                                         "bytes": row["bytes"]}, frames=[frame])


__all__ = ["FileSendTool", "SEND_ROOTS"]
