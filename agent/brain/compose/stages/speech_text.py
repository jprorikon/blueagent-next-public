"""句子切分與「還剩哪段沒唸」：大腦串流與語音出口共用。"""

from __future__ import annotations



#: 句末標點（日/中/英）——一句一合成，這個表就是要切的點。
_SENTENCE_END = "。．！？!?…\n"


#: 太短的碎片不切（「え。」「はい。」這種留著跟下一句併）。
_MIN_SENTENCE = 6


def _squash(text: str) -> str:
    """去掉所有空白（比對「唸過哪裡」時不看排版）。"""
    return "".join(str(text or "").split())


def _remaining_after_spoken(reply: str, spoken: str) -> str:
    """回覆裡「還沒被唸」的那一段。

    `spoken` 是串流時已經合成的句子（原文）。用去空白後的比較找邊界，
    回傳**原文切片**（保留原本的換行，交給 TTS 斷句）。
    整段都唸過就回空字串——這正是「重複兩遍」的來源。
    """
    if not spoken:
        return str(reply or "").strip()
    flat_reply, flat_spoken = _squash(reply), _squash(spoken)
    if not flat_reply.startswith(flat_spoken):
        return str(reply or "").strip()
    if flat_reply == flat_spoken:
        return ""
    # 在原文裡走 flat_spoken 的長度，找到對應的字元位置
    seen = 0
    for index, char in enumerate(str(reply or "")):
        if char.isspace():
            continue
        seen += 1
        if seen == len(flat_spoken):
            return str(reply or "")[index + 1:].strip()
    return ""


def _take_sentences(buffer: str) -> list[str]:
    """從緩衝裡取出「已經完整」的句子（含句末標點），不完整的留在 buffer。

    只有**太短的碎片**（「え。」「はい。」這種 ≤3 字）才往後併——不然一輪會冒出七八個氣音；
    正常句子一律照標點切（「おはよう。」5 個字也要切，不然首句優先等於沒做）。
    """
    out: list[str] = []
    start = 0
    for index, char in enumerate(buffer):
        if char not in _SENTENCE_END:
            continue
        piece = buffer[start:index + 1]
        if len(piece.strip()) <= 3 and index + 1 < len(buffer):
            continue                      # 碎片：併到下一句
        out.append(piece)
        start = index + 1
    return out
