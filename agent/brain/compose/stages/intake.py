"""進線：文字正規化、看圖（圖 → 文字描述）。"""

from __future__ import annotations

import pathlib
import traceback

from agent.brain.compose.stages.context import TurnContext


class NormalizeStage:
    name = "normalize"

    def run(self, env, ctx):
        TurnContext.of(env).intake.original = env.text
        env.text = " ".join(env.text.split()).strip()
        return None


class VisionStage:
    """看圖（`media.see` 的第一格）：圖 → 本地 VL → 文字，之後走打字那條。

    照舊橋 `ba/media/vision.py` 的做法：**圖不進腦的上下文**，只把 VL 讀出來的文字放進去
    （現役腦不吃圖；換腦時這層不用動）。讀不到就寫「讀不到」，不讓 VL 掛掉殺掉一整輪。
    """

    name = "vision"

    def run(self, env, ctx):
        payload = env.event.payload or {}
        media = payload.get("media") or {}
        # 延時聚合（owner 2026-11-04）：一輪可能帶好幾張（他連發的照片），逐張看、按順序貼。
        photos = [dict(item) for item in (payload.get("media_list") or []) if isinstance(item, dict)]
        if not photos and media:
            photos = [dict(media)]
        note = str(payload.get("text") or "").strip()
        blocks: list[str] = []
        last_path = ""
        for item in photos:
            path = str(item.get("path") or "")
            raw = item.get("bytes")
            data = bytes(raw) if isinstance(raw, (bytes, bytearray, memoryview)) else b""
            if path:
                last_path = path
            description = ""
            # App adapter 只交授权后的字节；旧 /api/image 仍可从私有路径读取。
            if not data and path and pathlib.Path(path).exists():
                try:
                    data = pathlib.Path(path).read_bytes()
                except OSError:
                    data = b""
            if data:
                try:
                    from agent.brain.media.vision import describe
                    description = describe(data, extra=note,
                                           mime=str(item.get("mime") or "image/png"))
                except Exception:                       # noqa: BLE001 - 看圖失敗不該殺這一輪
                    # 但**要留痕**：上一次靜默吞掉，害「她說看不到圖」只能靠猜
                    print("[vision] 看圖例外：\n" + traceback.format_exc(), flush=True)
                    description = ""
            elif path:
                print(f"[vision] 圖檔不存在或無法讀取：{path}", flush=True)
            blocks.append("[画像] " + (description if description else "（読めませんでした）"))
        block = "\n".join(blocks) if blocks else "[画像] （読めませんでした）"
        description = "".join(blocks)
        if note:
            block += f"\nオーナーの言葉：{note}"
        # 一行痕跡：這一輪到底把什麼樣的圖文塊交給了腦（看不到圖時就靠它分辨
        # 「VL 沒讀出來」還是「讀出來了但沒進 prompt」）
        print(f"[vision] entity={env.decision.entity if getattr(env, 'decision', None) else '?'} "
              f"張數={len(photos)} path={'有' if last_path else '無'} "
              f"desc={len(description)}字 block={block.splitlines()[0][:40]!r}", flush=True)
        intake = TurnContext.of(env).intake
        intake.image_note = note
        intake.image_path = last_path
        env.text = block
        return None
