"""委任：把出圖交給外部的委任先（只有主人、只在 work）。

委任先是哪個 agent 只有 adapter 那一層知道（`adapter/hermes_delegate`）；她看到的名字與說明一律中性
（owner 2026-11-11「不需要暴露给 blueagentnext 这边，委任对象是什么 agent」）。
"""

from __future__ import annotations

import pathlib

from agent.brain.runtime.delegate import RECEIPT_DONE_WORD, RECEIPT_TAG
from agent.brain.runtime.tools.base import ToolContext, ToolResult

#: 「這是出圖」的字眼（日／中／英）。委任只剩出圖（owner 2026-11-03）；描述寫了還不夠，代碼要擋（PENDING Agent.6）。
IMAGE_WORDS = ("画像", "絵", "描", "イラスト", "立ち絵", "壁紙", "アイコン", "ポスター", "出図", "生成画",  # lang-ok: 出圖判定詞表，不進 prompt
               "图", "圖", "画", "插画", "插畫", "海报", "海報", "头像", "頭像", "壁纸",  # lang-ok: 主人中文說法的判定詞，不進 prompt
               "image", "picture", "illustration", "draw", "paint", "render", "wallpaper", "icon",
               "comfyui", "sdxl", "stable diffusion")
IMAGE_OUTPUTS = ("{{out}}.png", "{{out}}.jpg", "{{out}}.jpeg", "{{out}}.webp")


def is_image_task(task: str) -> bool:
    """委任的任務是不是出圖：說了出圖的字眼，或把產物寫成 `{{OUT}}.png／.jpg／.webp`。"""
    text = str(task or "").lower()
    return any(word in text for word in IMAGE_OUTPUTS) or any(word in text for word in IMAGE_WORDS)


class DelegateTool:
    """`delegate`：把出圖交給委任先真去做（只有主人、只在 work 模式）。

    描述用日语——模型是读这段决定要不要调的（现役桥同一条经验：说明文跟人格同语言）。
    """

    name = "delegate"
    #: 舊名（歷史回合裡的工具呼叫還叫這個）⇒ 照樣認得，但不再出現在給她的清單上
    aliases = ("delegate.hermes", "delegate_hermes")
    #: 只在 work 模式出现；guest 看不到（第二道闸在 DelegateService 里）
    modes = ("work",)
    owner_only = True
    #: 还要这个能力被放行（＝user.md 的「权限」段 ＋ token 角色，两者都过才有）
    capability = "delegate"
    schema = {
        "type": "function",
        "function": {
            "name": "delegate",
            "description": (
                "**画像生成**を委任する。"
                "それ以外のこと——ファイル操作・調査・機械の操作・投稿・コードを書く——は"
                "自分の VM で `terminal` を使って自分でやる。委任で済ませない。"
                "画像生成には専用の出図の仕組みが要るので委任する。"
                "task にはオーナーが言った語（作品名・キャラ名・ファイル名など）を**一字も変えず**写すこと。"
                "出力ファイル名は自分で書かず `{{OUT}}` とだけ書き、拡張子は自分で付ける（`{{OUT}}.png`）。"
                f"返ってきた文の末尾に系統が `{RECEIPT_TAG}` を付ける——「{RECEIPT_DONE_WORD}」なら仕事は完了、"
                "同じ仕事を二度委任しない。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string",
                             "description": "委任先に渡す完全な指示（固有名詞は原文のまま）"},
                    "context": {"type": "string",
                                "description": "任意：補足の背景（オーナーの直前の要求など）"},
                },
                "required": ["task"],
            },
        },
    }

    #: 委任出來的圖存進 media store ⇒ 兩端（App／Discord）都用同一個 URL 拿
    IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")

    def __init__(self, delegate, media=None) -> None:
        self.delegate = delegate
        self.media = media

    def run(self, ctx: ToolContext, arguments: dict) -> ToolResult:
        task = str(arguments.get("task") or "").strip()
        if not is_image_task(task):
            # 不是出圖就不送委任先：讓她自己在 VM 上用 terminal 做（委任縮窄的硬判，PENDING Agent.6）
            return ToolResult(ok=False, error="delegate_image_only", data={
                "text": "委任は画像生成だけ。これは自分の VM で `terminal` を使ってやる。"})
        context = str(arguments.get("context") or "").strip()
        if context:
            task = f"{task}\n\n（背景：{context}）"
        result = self.delegate.run(task=task, principal=ctx.principal,
                                   thread_id=ctx.thread_id)
        payload = result.to_dict()
        if result.refused:
            return ToolResult(ok=False, data=payload,
                              error=f"delegate_refused:{result.refused}")
        frames: list[dict] = []
        # 產物：**圖**存進 media store 換成 URL（App 與 Discord 都靠這個幀，選路跟文字回覆同一條）；
        # 非圖的才退回 `artifact`（帶路徑，只有主人那端看得到）。
        for path in result.artifacts:
            frame = self._photo_frame(ctx, path)
            if frame is not None:
                frames.append(frame)
                continue
            frames.append({"kind": "artifact",
                           "payload": {"name": path.rsplit("/", 1)[-1], "path": path,
                                       "kind": path.rsplit(".", 1)[-1] if "." in path else ""}})
        if result.ok:
            frames.append({"kind": "receipt",
                           "payload": {"text": result.receipt,
                                       "ok": bool(result.artifacts)}})
        return ToolResult(ok=result.ok, data=payload, frames=frames,
                          error=result.error)

    def _photo_frame(self, ctx, path: str):
        """圖的產物 → `photo` 幀（`{id, url, name}`）；存不進 media store 就回 None（退回 artifact）。

        為什麼要存進媒體倉（owner 2026-11-04「委任出圖需要同時實現 discord 和 app 兩邊」）：
        端末拿不到機器上的路徑，只認 `/api/media/<id>` 這個 URL；存進來之後
        App 用 SSE 幀畫圖、Discord 用同一個 id 取檔上傳附件，**兩邊同一份**。
        """
        if self.media is None or not str(path).lower().endswith(self.IMAGE_EXT):
            return None
        try:
            data = pathlib.Path(path).read_bytes()
            ext = "." + str(path).rsplit(".", 1)[-1].lower()
            row = self.media.save(data, ext=ext, principal=str(ctx.principal or ""),
                                  entity=str(ctx.entity or ""),
                                  name=path.rsplit("/", 1)[-1])
        except (OSError, ValueError) as exc:            # noqa: BLE001 - 存不進去不能擋交付
            print(f"[delegate] 產物存不進媒體倉（{type(exc).__name__}: {exc}）", flush=True)
            return None
        return {"kind": "photo",
                "payload": {"id": row["id"], "url": "/api/media/" + str(row["id"]),
                            "name": path.rsplit("/", 1)[-1], "bytes": row.get("bytes")}}


__all__ = ["DelegateTool", "is_image_task"]
