"""心情 → 話（情感整理 P7：以前散在 `affect._TONE`、心跳 `_TEMPLATES`、`heartbeat_compose.mood_note` 三處）。

docs/PLAN-affect-reorg-20261109.md §3-1／§3-3。三個消費方都從這裡拿：

  · `mood_block(mood, cause)`：回話 prompt 的「### いまの気分」（只給方向、不給數字）；
  · `mood_note(label, cause)`：心跳組句的「いまの気分」一行；
  · `template(label)`：沒有組句器時的模板台詞。

低落（しょんぼり／いらいら）的那一句**看原因主人看不看得見**（`mood_cause.main_cause`）：
看得見 ⇒ 照舊（不解釋、不遷怒）；看不見 ⇒ 要嘛一句帶過原因，要嘛不露——
以前回話這邊寫「理由を説明しない」、組句那邊寫「理由を一言添える」，兩條路說法相反。
"""

from __future__ import annotations

#: 沈んでいる側の気分
LOW_MOODS = frozenset({"しょんぼり", "いらいら"})

#: 情緒 → 語氣。**只給方向，不給數字**（契約：關係／情緒注入 ≤120 字，不搶人格）。
#: 每一條都帶「不要做什麼」——不讓她把情緒演成藉口（NO_GUILT_APPEAL）。
TONE = {
    "わくわく": "少し浮かれてる。声が弾み、言葉が軽くなる。\n"
                "（嬉しさを説明しない。誘い文句にもしない）",
    "ごきげん": "機嫌がいい。柔らかく、少し遊びがある話し方。\n"
                "（ご機嫌を報告しない。いつもより一言多いくらいでいい）",
    "いらいら": "少し苛立っている。言葉が短く、間が減る。\n"
                "（当たり散らさない。理由を説明しない）",
    "しょんぼり": "少し沈んでいる。声が小さく、間が長い。\n"
                  "（重くしない。慰めてとねだらない）",
    "ふつう": "落ち着いている。いつも通りでいい。",
}

#: 低落、而且原因主人看不見的時候換掉 TONE 括號裡那句（`{why}`＝原因的人話）
HIDDEN_LOW = {
    "いらいら": "少し苛立っている。言葉が短く、間が減る。\n"
                "（理由は相手に見えていない：{why}。八つ当たりしない。"
                "気分を出すなら一言だけ理由に触れる。相手のせいにしない）",
    "しょんぼり": "少し沈んでいる。声が小さく、間が長い。\n"
                  "（理由は相手に見えていない：{why}。重くしない、慰めてとねだらない。"
                  "気分を出すなら一言だけ理由に触れる）",
}

#: arousal 對語速的影響（只調「節奏」，不調「情緒」）。
PACE = ((0.6, "少し早口。文を短く。"), (0.15, ""), (-1.0, "ゆっくり。文を短く。"))

#: 沒有組句器時的模板（短、口語、不說教）。低落那兩組不用（說不出原因）⇒ `template()` 退到 ふつう。
TEMPLATES = {
    "わくわく": ("ねえ、ちょっといい？", "いま、ちょっといいことがあって"),
    "ごきげん": ("オーナー、元気？", "なんとなく、声が聞きたくなった"),
    "しょんぼり": ("……ちょっとだけ、話してもいい？", "なんか、ひとりだと静かすぎる"),
    "いらいら": ("ねえ、聞いて", "ちょっと、うまくいかないことがあって"),
    "ふつう": ("あのね", "いま、なにしてる？"),
}


def _hidden_low(label: str, cause: dict | None) -> bool:
    return label in LOW_MOODS and bool((cause or {}).get("hidden"))


def mood_block(mood: dict, *, cause: dict | None = None, max_chars: int = 120) -> str:
    """把 `mood()` 的結果翻成**給模型看的語氣指示**（很短，不給數字）。"""
    if not mood:
        return ""
    label = str(mood.get("label") or "ふつう")
    if _hidden_low(label, cause):
        tone = HIDDEN_LOW[label].format(why=str(cause.get("words") or "自分のこと"))
        max_chars += 80                   # 多出來的是「原因看不見」那句，不搶人格
    else:
        tone = TONE.get(label, TONE["ふつう"])
    pace = ""
    arousal = float(mood.get("arousal") or 0.0)
    for threshold, hint in PACE:
        if arousal >= threshold:
            pace = hint
            break
    body = f"{tone}\n{pace}".strip() if pace else tone
    return f"### いまの気分\n{body}"[:max_chars + 40]


def mood_note(label: str, cause: dict | None = None) -> str:
    """心跳組句 prompt 的「いまの気分」一行（主動開口：低落一律要交代原因或不露）。"""
    if not label:
        return ""
    if label in LOW_MOODS:
        why = str((cause or {}).get("words") or "")
        reason = f"（理由：{why}）" if why else ""
        return (f"（いまの気分：{label}{reason}。その理由はオーナーには見えていない。"
                "気分を出すなら理由を一言添える、添えないなら出さない。オーナーのせいにしない、"
                "寂しさで返事を求めない）")
    return f"（いまの気分：{label}）"


def template(label: str) -> tuple[str, str]:
    """模板台詞（頭, 身）；低落的不用（說不出原因）。"""
    key = str(label or "ふつう")
    if key in LOW_MOODS:
        key = "ふつう"
    return TEMPLATES.get(key, TEMPLATES["ふつう"])


#: 群聊被 `@` 時拿起率要乘的心情係數（GROUP-SELF-CHAT §12-3 的口子，情感整理 §3-8）
CALLED_FACTOR = {"わくわく": 1.0, "ごきげん": 1.0, "ふつう": 1.0, "いらいら": 0.9, "しょんぼり": 0.85}


def called_factor(label: str) -> float:
    return CALLED_FACTOR.get(str(label or "ふつう"), 1.0)


__all__ = ["CALLED_FACTOR", "HIDDEN_LOW", "LOW_MOODS", "PACE", "TEMPLATES", "TONE",
           "called_factor", "mood_block", "mood_note", "template"]
