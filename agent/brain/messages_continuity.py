"""本輪段的「承接」兩塊：`[[before]]`（這條線上一段的摘要）與 `[[elsewhere]]`（別條線剛剛聊的）。

從 `messages.py` 拆出來（那支在 600 行棘輪邊上）；兩塊都只放本輪段（歷史之後），不動固定段的前綴快取。

`[[elsewhere]]`＝D2 跨線短期餘溫（owner 2026-11-17：「帶上餘溫，但是需要標註渠道來源」）。
資料由 `ThreadStore.elsewhere` 給（已按「這一場的人 ⊆ 那條線的人」過濾過）；這裡只負責
**每一段都寫清楚是哪個渠道**——她要知道那是 Discord 語音還是 App 私聊，才不會把那邊的話
當成這邊剛說的、或在群裡提起只有兩個人知道的口氣。
"""

from __future__ import annotations

from typing import Mapping

from agent.brain.domain.time import human_age

#: 渠道 → 她看得懂的標籤（`runtime/tools/channels.channel_of` 的值）。認不得的照原樣寫來源前綴。
CHANNEL_LABELS = {
    "app": "App",
    "call": "App の通話",
    "discord": "Discord",
    "discord_vc": "Discord のボイスチャンネル",
}


def channel_label(source: str, *, group: bool) -> str:
    """`phone:owner` → 「App の個別チャット」、`discord:vc…` → 「Discord のボイスチャンネル」。"""
    raw = str(source or "").strip().lower()
    if raw.startswith(("phone:call", "mr:call")):
        channel = "call"
    elif raw.startswith("discord:vc"):
        channel = "discord_vc"
    else:
        head = raw.split(":", 1)[0]
        channel = ("app" if head in ("", "phone", "api", "mr", "app")
                   else "discord" if head == "discord" else head)
    label = CHANNEL_LABELS.get(channel, channel or "別の場所")
    if channel in ("app", "discord"):
        label += " のグループ" if group else " の個別チャット"
    return label


def _elsewhere_block(sections, *, names: Mapping, moment: float, entity_name: str) -> str:
    parts = []
    for section in sections or ():
        rows = [row for row in section.get("rows") or () if str(row.get("text") or "").strip()]
        if not rows:
            continue
        group = bool(section.get("group"))
        fallback = str(section.get("source") or "")
        lines, label = [], None
        for row in rows:
            # 一對一的線跨 App／Discord 共用（7.6），同一段裡可能換過渠道 ⇒ 渠道一變就重新標一次
            here = channel_label(str(row.get("source") or fallback), group=group)
            if here != label:
                label = here
                at = float(row.get("at") or section.get("at") or 0.0)
                lines.append(f"〔{label}・{human_age(moment - at, now=moment)}〕" if at else f"〔{label}〕")
            if str(row.get("role") or "") == "assistant":
                who = "あなた"
            else:
                speaker = str(row.get("speaker") or "")
                who = names.get(speaker, speaker) or "相手"
            lines.append(f"- {who}：{str(row.get('text') or '').strip()}")
        parts.append("\n".join(lines))
    if not parts:
        return ""
    return ("[[elsewhere]]\n少し前に**別の場所**で" + (entity_name or "あなた")
            + "が話していたこと（ここにいる人はそこにもいた）。〔〕がどの場所か。\n"
            "続きとして自然に触れてよいが、この場で今言われたことにしない。"
            "場所が違うことを踏まえ、逐語で繰り返さない。\n"
            + "\n\n".join(parts) + "\n[[/elsewhere]]")


def continuity_blocks(meta: Mapping, *, names: Mapping, moment: float) -> list[dict]:
    """`meta["carry_summary"]` ⇒ `[[before]]`；`meta["elsewhere"]` ⇒ `[[elsewhere]]`。"""
    blocks: list[dict] = []
    carry = str(meta.get("carry_summary") or "").strip()
    if carry:
        blocks.append({"role": "system", "content": (
            "[[before]]\n以下は**前の区切りの会話**の要約（この回の発言ではない）。"
            "続きとして自然に使う。逐語で繰り返さない。\n" + carry + "\n[[/before]]")})
    elsewhere = _elsewhere_block(meta.get("elsewhere") or (), names=names, moment=moment,
                                 entity_name=str(meta.get("entity_name") or ""))
    if elsewhere:
        blocks.append({"role": "system", "content": elsewhere})
    return blocks


__all__ = ["CHANNEL_LABELS", "channel_label", "continuity_blocks"]
