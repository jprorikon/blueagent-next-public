"""「進 prompt 的文字必須是日語」的共用判據（給靜態掃描與渲染級測試共用）。

不用寬泛的 CJK 正則：日文漢字會被誤殺。改用兩組人工校過的字表——

  · `STRICT`：日文**不用**的字形（簡體專用＋與日文新字體不同的繁體），出現一個就算；
  · `LOOSE`：中文高頻、日文偶爾也用（的・了・我・什），同一段裡湊到兩個才算。

`string_constants()` 用 `tokenize` 取**完整**字串常量（多行模板也算），跳過 docstring 與
日誌呼叫（`print(`／`_log(`／`log(` 的引數）——那些不進 prompt。
語句（或該行）帶 `# lang-ok` 註解的也跳過：比對用的中文詞表、存檔的鍵這類**不進 prompt** 的資料，
要在原處寫明理由。
"""

from __future__ import annotations

import io
import pathlib
import tokenize

STRICT = set("你她这说时对么个为还过问边发确认编历轮话记忆请们吗呢没让谁"
             "执务关开门无产应该设单选项图读动处现线东车长间习结给种见觉视论语识页题颜风马鱼头归买卖带导帮"
             "這說對麼邊發歷們嗎讓經從會實體舊與輕寫聽點樣條歡")
LOOSE = set("的了我什")
_LOG_CALLS = {"print", "_log", "log"}


def chinese_hits(text: str) -> list[str]:
    """回命中的那幾行（空 list ＝乾淨）。"""
    hits = []
    for line in str(text or "").splitlines() or [str(text or "")]:
        strict = {ch for ch in line if ch in STRICT}
        loose = {ch for ch in line if ch in LOOSE}
        if strict or len(loose) >= 2:
            hits.append(line.strip()[:80])
    return hits


def string_constants(path: pathlib.Path) -> list[tuple[int, str]]:
    """`(行號, 字串內容)`：完整字串常量（含多行、f-string 的字面部分），不含 docstring／日誌引數。"""
    source = path.read_text(encoding="utf-8")
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    out: list[tuple[int, str]] = []
    allowed = {token.start[0] for token in tokens
               if token.type == tokenize.COMMENT and "lang-ok" in token.string}
    depth_in_log = []                       # 目前所在的括號層，是不是日誌呼叫
    previous_significant = None
    previous_name = ""
    statement_line = 1
    for token in tokens:
        kind, text = token.type, token.string
        if previous_significant in (None, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT) \
                and kind not in (tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT):
            statement_line = token.start[0]
        if kind == tokenize.OP and text in "([{":
            depth_in_log.append(text == "(" and previous_name in _LOG_CALLS)
        elif kind == tokenize.OP and text in ")]}":
            if depth_in_log:
                depth_in_log.pop()
        elif kind in (tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", -1)):
            in_log = any(depth_in_log)
            is_docstring = (kind == tokenize.STRING and previous_significant in
                            (None, tokenize.INDENT, tokenize.NEWLINE, tokenize.DEDENT)
                            and not depth_in_log)
            pragma = statement_line in allowed or token.start[0] in allowed
            if not in_log and not is_docstring and not pragma:
                out.append((token.start[0], _body(text) if kind == tokenize.STRING else text))
        if kind not in (tokenize.NL, tokenize.COMMENT):
            previous_significant = kind
            previous_name = text if kind == tokenize.NAME else ""
    return out


def _body(literal: str) -> str:
    """去掉引號與前綴（r／b／f／u）。"""
    stripped = literal.lstrip("rRbBfFuU")
    for quote in ('"""', "'''", '"', "'"):
        if stripped.startswith(quote) and stripped.endswith(quote) and len(stripped) >= 2 * len(quote):
            return stripped[len(quote):-len(quote)]
    return stripped


__all__ = ["LOOSE", "STRICT", "chinese_hits", "string_constants"]
