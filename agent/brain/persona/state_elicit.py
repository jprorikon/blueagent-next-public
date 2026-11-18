"""狀態引出（SPIN 第 ② 段）：用**人格 core × 當前情境**推出這一刻的心理狀態。

和 P0 的關係：
  · P0 編譯出 core（她是誰，一次、快取）；
  · 這裡是每次（**只在需要的場合**）問一次「這個 core 遇到這個情境會怎麼讀」，
    產出的是 transient state：什麼線索變顯著、哪條傾向被激活、情緒、顧慮、該不該開口。

為什麼只在需要的場合跑：這是多一次 LLM 呼叫（約 0.5–1.5 s）。私聊 VC 每輪都跑會拖延遲，
  真正需要「讀空氣」的是群聊／多人／TRPG——所以 `should_elicit()` 只放這幾種進來。

輸出（嚴格 JSON，壞了就當沒有）：
  {"salient_cues": [..], "active_dispositions": [{"trigger","tendency"}],
   "emotion": {"label","intensity","valence"}, "concerns": [..],
   "social_condition": "informational|normative|mixed",
   "intent": "answer|ask_back|act|stay_quiet|chime_in", "stance_hint": "一句話"}
"""

from __future__ import annotations

import json
import re
import time

CONDITIONS = ("informational", "normative", "mixed")
INTENTS = ("answer", "ask_back", "act", "stay_quiet", "chime_in")

PROMPT = """あなたは心理状態の推定器です。以下が与えられます。
1. PERSONALITY CORE（この人の安定した性格。ここに書かれたことだけを「この人」として扱う）
2. SITUATION（いま起きていること：相手の発言、場の種類、直前のやりとり）

この core が**この場面でどう反応するか**だけを推定してください。最終的な返答文は書かないこと。

ルール：
- 性格を推定し直さない（core をそのまま使う）。
- 目立つ手がかりを最大 4 つ。
- 発火した傾向は core の disposition_rules から選ぶ（無ければ空配列）。
- 感情は label（日本語 1 語）＋ intensity（0–1）＋ valence（-1–1）。
- social_condition は、相手が**事実を尋ねている**なら informational、
  **場に向けて意見・評価を述べている**なら normative、両方なら mixed。
- intent は answer / ask_back / act / stay_quiet / chime_in のどれか。
- stance_hint は 40 字以内の日本語（どう出るか。台詞そのものではない）。
- 厳密な JSON のみ。

出力スキーマ：
{{"salient_cues": ["..."], "active_dispositions": [{{"trigger": "...", "tendency": "..."}}],
 "emotion": {{"label": "...", "intensity": 0.0, "valence": 0.0}},
 "concerns": ["..."], "social_condition": "informational|normative|mixed",
 "intent": "answer|ask_back|act|stay_quiet|chime_in", "stance_hint": "..."}}

PERSONALITY CORE：
{core}

SITUATION：
{situation}
"""


def _clamp(value, low: float, high: float, default: float = 0.0) -> float:
    try:
        return round(min(high, max(low, float(value))), 3)
    except (TypeError, ValueError):
        return default


def parse_state(raw: str) -> dict | None:
    """解析模型輸出；形狀不對回 None（寧可沒有狀態，也不要半個）。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    emotion = data.get("emotion") if isinstance(data.get("emotion"), dict) else {}
    condition = str(data.get("social_condition") or "").strip().lower()
    if condition not in CONDITIONS:
        condition = "mixed"
    intent = str(data.get("intent") or "").strip().lower()
    if intent not in INTENTS:
        intent = "answer"
    cues = [str(row)[:40] for row in (data.get("salient_cues") or []) if str(row).strip()][:4]
    concerns = [str(row)[:40] for row in (data.get("concerns") or []) if str(row).strip()][:3]
    dispositions = []
    for row in (data.get("active_dispositions") or []):
        if not isinstance(row, dict):
            continue
        tendency = str(row.get("tendency") or "").strip()
        if not tendency:
            continue
        dispositions.append({"trigger": str(row.get("trigger") or "").strip()[:32],
                            "tendency": tendency[:60]})
    label = str(emotion.get("label") or "").strip()[:20]
    if not any((cues, dispositions, label, str(data.get("stance_hint") or "").strip())):
        return None
    return {
        "salient_cues": cues,
        "active_dispositions": dispositions[:3],
        "emotion": {"label": label,
                    "intensity": _clamp(emotion.get("intensity"), 0.0, 1.0, 0.3),
                    "valence": _clamp(emotion.get("valence"), -1.0, 1.0, 0.0)},
        "concerns": concerns,
        "social_condition": condition,
        "intent": intent,
        "stance_hint": str(data.get("stance_hint") or "").strip()[:60],
        "at": time.time(),
    }


def render_situation(*, text: str, scene: str = "group", recent: list[str] | None = None,
                     mood: str = "") -> str:
    """給模型看的「情境」——短、有結構，不含整份 transcript。"""
    rows = [f"場：{scene}", f"相手の発言：{str(text or '').strip()[:600]}"]
    if mood:
        rows.append(f"いまの気分：{mood}")
    tail = [str(row).strip()[:200] for row in (recent or []) if str(row).strip()][-3:]
    if tail:
        rows.append("直前のやりとり：\n" + "\n".join(f"・{row}" for row in tail))
    return "\n".join(rows)


def should_elicit(*, group: bool, source: str = "", mode: str = "",
                  enabled: bool | None = None) -> bool:
    """要不要跑這一段（省延遲）：群聊／TRPG／多人 才跑；私聊不跑。

    `BLUEAGENT_STATE_ELICIT=0` 可整條關掉（回退）；`=all` 則連私聊也跑（除錯用）。
    """
    import os
    flag = str(os.environ.get("BLUEAGENT_STATE_ELICIT") if enabled is None
               else ("1" if enabled else "0")).strip().lower()
    if flag in {"0", "off", "false", "no"}:
        return False
    if flag in {"all", "always"}:
        return True
    return bool(group or str(source or "").startswith("trpg"))


def elicit_state(core: dict, *, text: str, scene: str = "group",
                 recent: list[str] | None = None, mood: str = "", call=None,
                 log=None, timeout: float = 25.0) -> dict | None:
    """跑一次狀態引出。`call(prompt) -> str` 由呼叫端給（方便測試／換模型）。"""
    say = log or (lambda _message: None)
    if not core or call is None:
        return None
    # 只送「肖像 ＋ 最強 3 條傾向」：trait 數字對「這一刻怎麼讀」沒有幫助，
    # 送整份 JSON 只會讓 2 s 變 5 s（2026-10-22 實測）。
    core_view = {
        "portrait": str(core.get("portrait") or "")[:300],
        "disposition_rules": [
            {"trigger": row.get("trigger"), "tendency": str(row.get("tendency") or "")[:60]}
            for row in (core.get("disposition_rules") or [])[:3]
        ],
    }
    prompt = PROMPT.format(
        core=json.dumps(core_view, ensure_ascii=False)[:900],
        situation=render_situation(text=text, scene=scene, recent=recent, mood=mood),
    )
    started = time.monotonic()
    try:
        raw = call(prompt)
    except Exception as exc:                            # noqa: BLE001 - 引出失敗不擋對話
        say(f"state: 引出失敗（{type(exc).__name__}: {exc}）")
        return None
    spent = time.monotonic() - started
    state = parse_state(raw)
    if state is None:
        say(f"state: 輸出形狀不對（{str(raw)[:60]!r}）")
        return None
    state["elapsed_ms"] = int(spent * 1000)
    if spent >= 5.0:
        say(f"state: 慢（{spent:.1f}s）— 檢查是不是又被思考型模型吃掉")
    return state


def prompt_block(state: dict | None, *, max_chars: int = 400) -> str:
    """給回覆 prompt 的那一小段：**她的讀法**，不是 JSON。

    只放「什麼變顯著、什麼情緒、打算怎麼出」——不放 trait 數字（她不需要知道自己幾點幾分）。
    """
    if not state:
        return ""
    lines: list[str] = []
    cues = state.get("salient_cues") or []
    if cues:
        lines.append("目に入ったこと：" + "／".join(cues[:3]))
    emotion = state.get("emotion") or {}
    label = str(emotion.get("label") or "").strip()
    intensity = float(emotion.get("intensity") or 0.0)
    if label:
        level = "強め" if intensity >= 0.7 else "ふつう" if intensity >= 0.4 else "かすか"
        lines.append(f"いまの気持ち：{label}（{level}）")
    concerns = state.get("concerns") or []
    if concerns:
        lines.append("気にかかっていること：" + "／".join(concerns[:2]))
    hint = str(state.get("stance_hint") or "").strip()
    if hint:
        lines.append(f"出方：{hint}")
    if not lines:
        return ""
    return "### この場面の読み\n" + "\n".join(lines)[:max_chars]


__all__ = ["CONDITIONS", "INTENTS", "elicit_state", "parse_state", "prompt_block",
           "render_situation", "should_elicit"]
