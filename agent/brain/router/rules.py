"""路由規則：**能算的先用算的**，LLM 只在必要時、且在非實時路徑上才進來。

分流要滿足三個互相拉扯的目標：

  1. **穩定**：同一句話重放要得到同一個決策（可回放、可驗收、可解釋）。
  2. **低延遲**：語音第一幀的預算只有幾百毫秒，路由不能卡在這裡。
  3. **懂場景**：跨端、跨場景、切角色、打斷、排隊，都要按同一套規則。

所以規則是分層的：
  確定性表（源分類／目的／連續窗／模式／優先級）→ 可選分類器（只給文字、且非實時）
  → 保底藍圖。分類器是「加分項」，永遠不是必要條件。
"""

from __future__ import annotations

from typing import Callable

from agent.brain.domain.decision import DispatchMode, Engagement
from agent.brain.domain.event import IngressEvent, InputKind, Origin, SourceClass
from agent.brain.domain.thread import Purpose, Thread
from agent.brain.pipelines import blueprints

#: 沒有場景資訊時落這裡。單主人階段「手機打字」與「Discord 一對一」都應該落在同一條線，
#: 這樣換端不會感覺換了一個人；有第三者在場或換到桌上時，源會給更具體的值。
DEFAULT_SCENE = "together"

#: 連續窗：多久之內同一個 (principal, entity, scene, purpose) 算同一段對話。
#: 實時源給最長（口語本來就有停頓），環境源最短（它本來就該即時收斂）。
WINDOWS: dict[SourceClass, float] = {
    SourceClass.REALTIME: 30 * 60.0,
    SourceClass.INTERACTIVE: 15 * 60.0,
    SourceClass.AMBIENT: 5 * 60.0,
}

#: 首幀預算（毫秒）。None＝不承諾（環境源）。
#: ⚠️ 這裡的數字是 **LLM 首字預算**（從送出請求到第一個正文／工具 token），**不是 E2E**；
#:    它同時被當成連線＋首字節的 `open_timeout`（`brain/openai_compat.py:342`）。
#: 2026-11-08 owner 拍板把 REALTIME 1500 → **3000**：同一份 ~15.6k 字（≈10k token）prompt，
#: deepseek-flash 首字實測 1221／1244／1554 ms（三次），1500 剛好壓線 ⇒ 通話常常一個字都沒回。
#: 3 s 留了餘量；通話真正的 E2E（STT＋recall＋生成＋TTS＋投遞）另有 30 s 的整輪等待上限。
DEADLINES: dict[SourceClass, int | None] = {
    SourceClass.REALTIME: 3000,
    SourceClass.INTERACTIVE: 8000,
    SourceClass.AMBIENT: None,
}

_PURPOSE_WEIGHT = {Purpose.CHAT: 0, Purpose.WORK: 1, Purpose.TRPG: 2,
                   Purpose.GAME: 3, Purpose.AMBIENT: 4}


def scene_for(event: IngressEvent) -> str:
    return str(event.scene or "").strip() or DEFAULT_SCENE


def purpose_for(event: IngressEvent) -> Purpose:
    raw = str(event.payload.get("purpose") or "").strip().lower()
    if raw:
        try:
            return Purpose(raw)
        except ValueError:
            pass
    if event.source.startswith(("game:", "game-")):
        return Purpose.GAME
    if event.source.startswith(("trpg:", "trpg-")):
        return Purpose.TRPG
    if event.source_class is SourceClass.AMBIENT:
        return Purpose.AMBIENT
    return Purpose.CHAT


def continuity_window(cls: SourceClass) -> float:
    return float(WINDOWS.get(cls, 600.0))


def priority_for(cls: SourceClass, purpose: Purpose) -> int:
    """數值愈小愈優先。源分類是主序，目的只是同級內的微調（桌／遊戲略讓位給主線）。"""
    return int(cls) * 10 + _PURPOSE_WEIGHT.get(purpose, 0)


def deadline_for(cls: SourceClass) -> int | None:
    return DEADLINES.get(cls)


def engage_for(event: IngressEvent, *, group: bool, wake: bool = False,
               reply_to_group: bool = False) -> Engagement:
    """她要不要接这句话。**确定性规则优先**，群聊里默认安静旁听。

    `addressed_to` 由源给（Discord 的 mention/reply、DM、TRPG 点名……）；
    `wake` 是文本层的唤醒词兜底（她自己的名字）。
    """
    target = str(event.addressed_to or "").strip().lower()
    if target in {"ignore", "noise"}:
        return Engagement.IGNORE
    # 無主源：她自己發起的活動（TRPG 輪次、自己的行程）。沒有人「對她說」，
    # 她本來就該行動；除非源明確標成 ignore。
    if event.origin is Origin.SELF:
        return Engagement.REPLY
    if target == "entity":
        return Engagement.REPLY
    if not group:
        return Engagement.REPLY
    if target == "group":
        return Engagement.REPLY if reply_to_group else Engagement.OBSERVE
    if target and target not in {"unknown", "any"}:
        return Engagement.OBSERVE          # 明确在对另一个 principal 说
    return Engagement.REPLY if wake else Engagement.OBSERVE


def choose_mode(event: IngressEvent, thread: Thread) -> DispatchMode:
    """有沒有東西正在跑？誰有資格插隊？——整段併發語義只在這一個函式裡。"""
    if thread.inflight:
        if event.source_class is SourceClass.REALTIME:
            return DispatchMode.INTERRUPT
        if event.source_class is SourceClass.INTERACTIVE:
            return DispatchMode.QUEUE
        return DispatchMode.DROP
    if event.source_class is SourceClass.AMBIENT and thread.queued:
        return DispatchMode.DROP
    return DispatchMode.ATTACH


def pipeline_for(event: IngressEvent, purpose: Purpose,
                 *, classifier: Callable[[IngressEvent], str | None] | None = None) -> tuple[str, str]:
    """回 `(管線 id, 理由)`。理由會進 route 日誌，這是「她為什麼回這邊」的證據。"""
    if event.pipeline_hint:
        return event.pipeline_hint, "hint"
    # 分類器只准在「文字 × 非實時」上用：語音路徑不能被一次模型呼叫拖慢。
    if (classifier is not None and event.kind is InputKind.TEXT
            and event.source_class is not SourceClass.REALTIME):
        picked = classifier(event)
        if picked:
            return str(picked), "classifier"
    return blueprints.default_pipeline(event.source_class, event.kind, purpose.value), "default"
