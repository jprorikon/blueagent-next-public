"""`sticker.send`：她自己挑一張表情包（スタンプ）回過去（owner 2026-10-29「雙向」）。

只在**看得到圖的端**出現（App 的聊天畫面）：Discord／TRPG／通話中都看不到圖，
給了她會以為自己送出去了。可用的那幾張按**她自己的那一包**列進 schema（每張附意思），
所以 schema 是按角色現算的（`schema_for`），不是一張固定表。

目錄（`media/stickers.py` 的 `StickerCatalog`）由組合根注入；runtime 不 import media。
"""

from __future__ import annotations

from agent.brain.runtime.tools.base import ToolContext, ToolResult

#: 看得到表情的端：App 打字（`phone:<principal>`）與傳圖（`api:image`）。
#: `api:audio` 不在內：主人的語音回合是「先短回一句、後續交付」，後續那條只推文字。
STICKER_SOURCES: tuple[str, ...] = ("phone:", "api:image")
#: 通話是純聲音：送了也看不到
NO_STICKER_SOURCES: tuple[str, ...] = ("phone:call",)


def sticker_visible(source: str) -> bool:
    raw = str(source or "").strip().lower()
    return raw.startswith(STICKER_SOURCES) and not raw.startswith(NO_STICKER_SOURCES)


class StickerSendTool:
    name = "sticker.send"
    #: 只有 App 的聊天畫面看得到表情包
    channels = ("app",)
    #: 沒有固定 schema（`ToolRegistry.schemas` 會改問 `schema_for`）；給個空殼讓別名表認得名字
    schema = {"type": "function", "function": {"name": "sticker_send"}}

    def __init__(self, catalog) -> None:
        self.catalog = catalog

    def schema_for(self, *, entity: str, source: str) -> dict | None:
        if not sticker_visible(source):
            return None
        stickers = self.catalog.list(entity)
        if not stickers:
            return None
        menu = "\n".join(f"- {item.id}：{item.meaning}" for item in stickers)
        return {
            "type": "function",
            "function": {
                "name": "sticker_send",
                # ⚠️ **措辭會直接決定模型會不會用**（2026-10-29 真機實測）：
                # 原本寫「気持ちを絵で伝えたいときだけ（毎回は送らない）」⇒ 實測 **12/12 輪都不發**
                # （她每一輪都正確理解了主人那張貼圖的意思，就是從不主動回）。
                # 改成「**這是你表達情緒的管道之一、該用就該用**」⇒ 實測會發。
                "description": (
                    "スタンプ（表情）を一枚送る。**今の気持ちに合うものなら、"
                    "返事の言葉と一緒に送っていい。**\n"
                    "相手がスタンプを送ってきたときは、**あなたはスタンプで返す**"
                    "（言葉だけの返事より、そのほうが自然）。\n"
                    "使う場面の例：寝る前・食事・びっくりしたとき・冗談の返し・"
                    "「すごい」「ありがとう」の気持ちなど、"
                    "**言葉だけでは足りない気持ち**を絵で返すとき。\n"
                    "命令や質問には使わない；文面の答えは本文で返す。\n"
                    "返事の文と一緒に呼んでもいい（文中にスタンプのことは書かない）。\n"
                    "使えるスタンプ：\n" + menu),
                # id 不再列 enum：清單裡已經有，一包上百張時重複一次就多一千多字的 prompt；
                # 叫錯的 id 由 `run()` 擋（`unknown_sticker`）。
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "string", "description": "上の一覧の id"}},
                    "required": ["id"],
                },
            },
        }

    def run(self, ctx: ToolContext, arguments: dict) -> ToolResult:
        if not sticker_visible(ctx.source):
            return ToolResult(ok=False, error="sticker_not_visible_here")
        sticker = self.catalog.get(ctx.entity, str(arguments.get("id") or ""))
        if sticker is None:
            return ToolResult(ok=False, error="unknown_sticker")
        payload = sticker.to_dict()
        return ToolResult(ok=True, data={"sticker": sticker.id, "meaning": sticker.meaning},
                          frames=[{"kind": "sticker", "payload": payload}])


__all__ = ["StickerSendTool", "sticker_visible"]
