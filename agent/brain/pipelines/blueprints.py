"""管線藍圖：**宣告**一條管線由哪些階段組成；實作由插件提供。

這裡只放「形狀」，不放實作。組合根（未來的 bootstrap）負責把同名 stage 註冊進來；
router 只看管線 id，runtime 只會 `PipelineRegistry.require()`。

好處：
  · 換一條管線的組成＝改一張表，不是改 `if`。
  · 同一個 stage 可以被多條管線重用（例如 `recall` 同時出現在語音與打字）。
  · 測試可以用假 stage 組出同一條藍圖，驗證順序與取消，而不碰真模型。
"""

from __future__ import annotations

from typing import Mapping

from agent.brain.domain.event import InputKind, SourceClass
from agent.brain.pipelines.base import Pipeline, Stage

#: 管線 id → 有序 stage 名。名稱是插件鍵，不是類名。
BLUEPRINTS: dict[str, tuple[str, ...]] = {
    # 實時語音：門檻（喚醒）→ 糾錯 → 快速記憶 → 人格 → 串流腦 → 串流嘴 → 回傳
    "voice.realtime": ("gate", "stt_fix", "recall", "subagents", "persona", "brain.stream",
                       "voice.stream", "egress.voice"),
    # 打字：正規化 → 記憶 → 人格 → 腦 → 產物 → 回傳
    "chat.text": ("normalize", "recall", "subagents", "persona", "brain", "artifact",
                  "egress.app"),
    "chat.work": ("normalize", "recall", "subagents", "capabilities", "persona", "brain.work",
                  "artifact", "egress.app"),
    # 環境／背景：不說話，只沉澱。切片封存也在這裡接（summary / facts）
    "ambient.note": ("normalize", "recall", "brain.silent", "episode.summarize", "memory.write"),
    # 桌上：桌況 → 記憶 → 人格 → 腦 → 回桌
    "trpg.turn": ("trpg.context", "recall", "subagents", "persona", "brain",
                  "episode.summarize", "egress.table"),
    "game.talk": ("game.board", "persona", "brain.brief", "egress.game"),
    # 看圖：先讀畫面，再走打字那條
    "media.see": ("vision", "normalize", "recall", "persona", "brain", "artifact", "egress.app"),
}


def default_pipeline(lane: SourceClass, kind: InputKind | str, purpose: str) -> str:
    """沒指定、也判不出意圖時的保底管線。純函式，方便測試。"""
    value = kind.value if isinstance(kind, InputKind) else str(kind)
    if purpose == "game":
        return "game.talk"
    if purpose == "trpg":
        return "trpg.turn"
    if value == "image":
        return "media.see"
    if lane is SourceClass.REALTIME or value == "audio":
        return "voice.realtime"
    if lane is SourceClass.AMBIENT or purpose == "ambient":
        return "ambient.note"
    if purpose == "work":
        return "chat.work"
    return "chat.text"


def build(blueprint: str, stages: Mapping[str, Stage]) -> Pipeline:
    """按藍圖把已註冊的 stage 組成一條 `Pipeline`。

    `stages` 是「名稱 → stage 實例」，由組合根一次建好（stage 可以是有狀態的服務物件）。
    缺 stage 就當場紅：管線是啟動期契約，不該拖到第一句話才炸。
    """
    names = BLUEPRINTS.get(blueprint)
    if names is None:
        raise KeyError(f"unknown blueprint: {blueprint}")
    missing = [name for name in names if name not in stages]
    if missing:
        raise KeyError(f"blueprint {blueprint} missing stages: {', '.join(missing)}")
    return Pipeline(id=blueprint, stages=tuple(stages[name] for name in names))
