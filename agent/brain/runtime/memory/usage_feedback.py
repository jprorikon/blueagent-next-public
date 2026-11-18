"""記憶使用量回饋：哪一輪真的用到了哪條記憶（owner 2026-11-16）。

為什麼要有這支：注意力場的 α／β／γ 標定需要「哪條記憶真的被用進回覆」這個真值。
2026-11-16 之前的狀況是全庫 `last_used_at` 一直是 0（`note_usage()` 從沒人呼叫）。

**判準（2026-11-16 晚修正，實測踩過）**：第一版只看「共用 ≥2 個 bigram」，
結果主人一句「こんばんは」也把 5 條遊戲筆記記成「用到」——日文的 です／ます／から／今日は
這種功能詞 bigram 太容易撞。現在三道閘：

1. **去功能詞**：`STOP_GRAMS` 裡的 bigram 不計（です／ます／から／けど／って…）。
2. **內容詞至少 2 個**，而且佔**這條事實自己**內容 bigram 的 **≥15%**（短招呼語覆蓋不到長事實就不算）。
   分母是事實、不是回覆（2026-11-16 複審）：除以回覆的話，同一條事實共用 5 個內容詞，
   8 字的回覆算用到、70 字的回覆就不算——越長的回覆越漏記，α／γ 的真值會偏向短回覆。
3. **每次最多 3 條**：寧可少記，不要讓一輪把整個候選池都標成用過。

仍**只記 used、不記 miss**（猜錯的 miss 會把好記憶壓下去，`usefulness = u/(u+m)`）。

呼叫點：`compose/stages/brain.py` 收到回覆之後（由 stage 從 `TurnContext` 取召回再傳進來，
runtime 不反向 import compose）。
"""

from __future__ import annotations

#: 共用幾個**內容** bigram 才算「用到」
MIN_CONTENT_GRAMS = 2
#: 共用的內容 bigram 要佔**事實**內容 bigram 的多少比例（短招呼語對長事實 ⇒ 覆蓋率自然低）
MIN_COVERAGE = 0.15
#: 一輪最多記幾條（防一次把候選池全標成用過）
MAX_FACTS = 3
#: 太短的事實不比（「はい」這種沒法判斷）
MIN_FACT_CHARS = 6
#: 日文功能詞 bigram：撞到不算內容重疊（2026-11-16 實測：「こんばんは」誤中 5 條）。
#: 「オーナー」（オー／ーナ／ナー）也算進來：那是她對主人的稱呼、幾乎每句都有，
#: 撞到它不能當作「想起了哪條記憶」的證據（否則稱呼類事實每輪都被記一次用到）。
STOP_GRAMS = frozenset({
    "です", "ます", "でし", "した", "して", "する", "から", "けど", "けれ", "って",
    "のは", "のが", "ので", "こと", "そう", "よう", "今日", "きょ", "うは", "わた",
    "あた", "たし", "オー", "ーナ", "ナー", "はい", "いい", "かな",
    "だね", "だよ", "んだ", "なん", "でも", "また", "まだ", "もう", "これ", "それ",
})


def used_texts(recall, reply: str) -> list[str]:
    """從這一輪召回的事實裡挑出「回覆真的用到的」那幾條（去掉 `〔私記〕` 前綴）。"""
    body = str(reply or "").strip()
    items = [str(item) for item in (recall or ())]
    if not body or not items:
        return []
    from agent.brain.memory.facts import grams
    wanted = grams(body)
    if not wanted:
        return []
    scored: list[tuple[int, str]] = []
    for item in items:
        plain = item.replace("〔私記〕", "").strip()
        if len(plain) < MIN_FACT_CHARS:
            continue
        content = grams(plain) - STOP_GRAMS
        shared = wanted & content
        if len(shared) < MIN_CONTENT_GRAMS:
            continue
        if len(shared) / max(1, len(content)) < MIN_COVERAGE:
            continue
        scored.append((len(shared), plain))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [text for _count, text in scored[:MAX_FACTS]]


def mark(ctx, reply: str, *, recall=None, entity: str = "", game: int | None = None) -> int:
    """把這一輪用到的記憶寫回 `utility`／`last_used_at`；回寫了幾條。

    `recall` 由呼叫端（`compose/stages/brain.py`）從 `TurnContext.of(env).recall` 取來傳入
    ——runtime 不許反向 import compose（`test_package_layering`）。
    `entity`／`game` 一起傳：只記**這個角色、這個場景**那一條（各角色各自學習）。
    """
    try:
        texts = used_texts(recall or (), reply)
        if not texts:
            return 0
        note = _note_usage_of(getattr(ctx, "ports", None))
        if note is None:
            return 0
        note(texts, used=True, entity=str(entity or "") or None, game=game)
        return len(texts)
    except Exception:                                  # noqa: BLE001 - 記帳壞了不能擋回覆
        return 0


def _note_usage_of(ports):
    """從 ports 往下找 `note_usage`（AppPorts → memory → memory.local）。"""
    node = ports
    for _depth in range(3):
        if node is None:
            return None
        method = getattr(node, "note_usage", None)
        if callable(method):
            return method
        node = getattr(node, "memory", None) or getattr(node, "local", None)
    return None


__all__ = ["MAX_FACTS", "MIN_CONTENT_GRAMS", "MIN_COVERAGE", "MIN_FACT_CHARS", "STOP_GRAMS",
           "mark", "used_texts"]
