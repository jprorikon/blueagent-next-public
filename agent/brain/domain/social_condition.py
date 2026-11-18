"""「他是在問我事實，還是在群裡公開表態」——SPIN 的 informational / normative 區分。

為什麼要有這個（owner 2026-10-22 的 P3）：
  群裡她會插話。真正的原因不是「分數門檻」，而是我們分不出這兩種情況：
    · **informational**：他在問一件事（「これ何？」「明日の予定は？」）⇒ 該答。
    · **normative**：他在對群裡公開表態／發表看法（「只用來玩遊戲真是浪費顯卡（」）⇒ 該聽，
      最多附和一句；搶著回答會顯得很不會讀空氣。
  論文（arXiv 2610.08328）在社會心理學實驗裡就是靠這個條件切換，行為才對得上人；
  多元無知（私下不同意、公開附和）本質上也是這件事。

刻意用**規則**而不是模型：
  · 這段跑在路由熱路徑上（每則群訊息都跑），不能加 1 秒延遲；
  · 規則可測、可解釋、可回退；模型那層（`persona/state_elicit.py`）只在她真的要回應時才跑，
    回來若與規則不同，會記在日誌裡當校準資料。
"""

from __future__ import annotations

#: 問句標記（informational 的正向證據）。
QUESTION_MARKS: tuple[str, ...] = ("?", "？", "か？", "かな", "の？", "？", "吗", "呢", "什麼", "什么")

#: 疑問詞／請求（informational）。
INTERROGATIVES: tuple[str, ...] = (
    "何", "なに", "どこ", "いつ", "だれ", "誰", "どう", "なんで", "なぜ", "いくつ", "どれ",
    "教えて", "調べて", "確認して", "してくれ", "できる", "ある？", "いる？",
    "多少", "几点", "几天", "哪里", "怎么", "为什么", "是不是", "能不能", "要不要",
)

#: 公開表態／評價（normative）。
EVALUATIVE: tuple[str, ...] = (
    "と思う", "とおもう", "べき", "だよね", "でしょ", "じゃない", "わけ", "って感じ",
    "みんな", "みんなは", "世間", "普通は", "一般的に",
    "我觉得", "感觉", "真是", "有点", "应该", "大家", "说实话", "说白了",
    "浪费", "可惜", "无聊", "厉害", "太", "笑", "（", "(",
)

#: 直接叫她的名字（不管哪種都算對她說）。
ADDRESSING: tuple[str, ...] = ("ミラ", "ミラ", "mira", "オーナー", "オーナー")

INFORMATIONAL = "informational"
NORMATIVE = "normative"
MIXED = "mixed"


def _hits(text: str, words) -> int:
    return sum(1 for word in words if word and word in text)


def classify(text: str, *, addressed: bool = False) -> str:
    """回 `informational` / `normative` / `mixed`。

    `addressed=True`（@她、引用回覆她）⇒ 一律 informational：既然點名了，就是在跟她說話。
    """
    body = str(text or "").strip()
    if not body:
        return MIXED
    if addressed:
        return INFORMATIONAL
    asking = _hits(body, QUESTION_MARKS) + _hits(body, INTERROGATIVES)
    judging = _hits(body, EVALUATIVE)
    if asking and judging:
        # 問句優先：有問號／疑問詞就是在問，只是順帶帶了評價
        return INFORMATIONAL if asking >= judging else MIXED
    if asking:
        return INFORMATIONAL
    if judging:
        return NORMATIVE
    return MIXED


def should_answer(condition: str, *, mentioned: bool, continuation: bool,
                  has_substance: bool) -> bool:
    """群聊裡「該不該接這句」的規則（取代原本只看時間窗的接話）。

    · 點名（@／引用回覆她）⇒ 接。
    · informational ⇒ 接（有人在問事）。
    · normative ⇒ **不接**（公開表態不該搶答；她可以事後用自己的節奏講）。
    · mixed ⇒ 只有在「她剛說完＋對方說的有內容」時接（維持對話流，但不搶話）。
    """
    if mentioned:
        return True
    if condition == INFORMATIONAL:
        return True
    if condition == NORMATIVE:
        return False
    return bool(continuation and has_substance)


def has_substance(text: str) -> bool:
    """有沒有內容（不是「。」「w」這種）。"""
    core = "".join(ch for ch in str(text or "") if ch.isalnum())
    return len(core) >= 4


__all__ = ["ADDRESSING", "EVALUATIVE", "INFORMATIONAL", "INTERROGATIVES", "MIXED",
           "NORMATIVE", "QUESTION_MARKS", "classify", "has_substance", "should_answer"]


# ── 「這句話是哪一種」：問句／依賴・指示／陳述（owner 2026-10-24）────────────────
# 為什麼要另一支：`classify()` 判的是「該不該回」，判不到「怎麼讀這句話」。
# 實測踩到的：オーナー 說「役割カードの話忘れて」（＝忘掉那件事吧），她回
# 「忘れちゃったの？…忘れたふりしてあげる」——把**指令**讀成**對方在陳述自己忘了什麼**。
# 規則優先（跑在熱路徑、可測、可回退）；認不出來就回空字，不硬猜。
REQUEST_MARKS: tuple[str, ...] = (
    # 日文：て形依賴／命令／請託
    "てください", "でください", "てほしい", "ないで", "なさい", "しろ", "やめろ",
    "忘れて", "やめて", "見て", "来て", "待って", "して", "してね", "してよ",
    "お願い", "たのむ", "頼む", "やっといて", "しておいて", "しといて",
    # 中文
    "幫我", "帮我", "把這", "把那", "去做", "你去", "別再", "别再", "不要", "記得", "记得",
    "忘掉", "刪掉", "删掉", "改成", "換成", "换成", "看一下", "說一下", "说一下",
    "吧", "しよう", "しよっか",
)
QUESTION_WORDS: tuple[str, ...] = (
    "？", "?", "吗", "嗎", "呢", "かな", "の？", "か？", "って何", "どう", "なんで",
    "なぜ", "どこ", "いつ", "だれ", "誰", "何", "なに", "いくつ", "どれ",
    "什么", "什麼", "哪里", "哪裡", "怎么", "怎麼", "为什么", "為什麼", "多少", "幾",
)

REQUEST = "request"
QUESTION = "question"
STATEMENT = "statement"


def utterance_kind(text: str) -> str:
    """這句話是「依賴・指示」「問句」還是「陳述」？認不出來回空字。

    規則是**證據式**的：先看問號／疑問詞（問句），再看依賴標記；
    兩者都有（「〜してくれる？」）算問句——那是在問，不是在命令。
    """
    body = str(text or "").strip()
    if not body:
        return ""
    if _hits(body, QUESTION_WORDS):
        return QUESTION
    if _hits(body, REQUEST_MARKS):
        return REQUEST
    if body.endswith(("。", "！", "!", ".", "よ", "ね")):
        return STATEMENT
    return ""


__all__ += ["QUESTION", "QUESTION_WORDS", "REQUEST", "REQUEST_MARKS", "STATEMENT",
            "utterance_kind"]
