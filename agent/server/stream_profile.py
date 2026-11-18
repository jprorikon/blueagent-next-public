"""App 流式回應的可選精簡表示；未協商的客戶端永遠拿原始契約。"""

from __future__ import annotations

from agent.brain.media import digest

PARTS_V1 = "parts-v1"


def slim_done_event(event: str, payload: dict, profile: str = "") -> dict:
    """`parts-v1` 的 `done` 不再重送已由 `part` 送過的正文。

    只在 HTTP 出口複製並精簡；brain／outbox 的完整權威結果不變。錯誤回合保留
    原文，因為它不一定有對應的 `part`。`text_hash` 讓客戶端日後能校驗拼回的正文。
    """
    if event != "done" or str(profile or "").strip() != PARTS_V1:
        return payload
    if not isinstance(payload, dict):
        return payload
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else None
    holder = inner if inner is not None else payload
    if holder.get("error"):
        return payload
    text = str(holder.get("text") or "")
    if text and not holder.get("bubbles"):
        # 分段器失敗時沒有 part 可還原；保留 done 全文當兜底，不能為瘦身丟回覆。
        return payload
    fresh = dict(holder)
    fresh.pop("text", None)
    fresh.pop("bubbles", None)
    fresh["text_hash"] = digest.content_tag(text.encode("utf-8"))
    fresh["stream_profile"] = PARTS_V1
    return dict(payload, payload=fresh) if inner is not None else fresh


__all__ = ["PARTS_V1", "slim_done_event"]
