#!/usr/bin/env python3
"""閘門：Next 端的界面文字（`Resources/i18n/next_{locale}.json` ＋ `Scripts/Next/*.cs`）。

查五件事（任何一件不過 ⇒ exit 1）：

1. 三份語言檔（ja／zh-Hans／zh-Hant）都在、是扁平的字串表、**沒有重複 key**、值不空。
   （重複 key 的歷史：2026-10-23 白屏事故——當時表寫在 C# 字典初始化器裡，重複 key 會讓
   靜態建構子丟 `TypeInitializationException`，症狀是殼畫出來了、body 全空。JSON 不會炸，但會
   靜靜地吃掉前一條，一樣要擋。）
2. 三份的 key 一模一樣，同一條的 `{佔位符}` 也一樣（少一個佔位符＝那種語言的畫面少一段資訊）。
3. **`Scripts/Next`／`Scripts/MR` 的字串常值裡不准有中日文**（2026-10-28 完整 i18n）：畫面上的字一律走
   `NextStrings`。`Debug.Log*` 那一整句不算（開發者日誌，不上畫面）。
4. 不准 emoji（🎲、✎、🎤…）：Noto Sans JP／SC／TC 沒有這些字形，畫出來是豆腐（2026-10-29 桌面日誌踩到）。
   表裡的字與 `Scripts/Next` 的字串常值都查。
5. 程式裡 `NextStrings.Get/F("key")` 用到的 key 都在表裡；表裡每一條都有人用
   （`err_`／`events_`／`badge_`／`cap_`／`bg_`／`ent_`／`mood_` 開頭的是代碼拼出來的，不算沒人用）。
6. unity-review U-8：界面文案只准跟 `NextConfig.UiLanguage`。`NextConfig.Language` 是語音辨識的
   `X-Lang`，以前一堆視圖拿它去查表 ⇒ 界面切成中文，標題／按鈕照樣是日語。

用法：`python3 tools/check_next_strings.py`（exit 0＝乾淨）
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
NEXT = ROOT / "BlueAgentUnity" / "Assets" / "Scripts" / "Next"
#: MR 端（Quest 3）已拆到頂層 /mr，仍與手機共用這套 i18n 表與檢查。
MR = ROOT.parents[1] / "mr" / "BlueAgentUnity" / "Assets" / "Scripts" / "MR"
I18N = ROOT / "BlueAgentUnity" / "Assets" / "Resources" / "i18n"
LOCALES = ("ja", "zh-Hans", "zh-Hant")
#: 由代碼拼出 key 的前綴（`NextStrings.Code(prefix, code)`／`ErrorKey`）
DYNAMIC_PREFIXES = ("err_", "events_", "badge_", "cap_", "bg_", "ent_", "mood_")   # mood_：心情徽章用伺服器的 key 拼

#: `NextStrings.Get(…, NextConfig.Language)`：界面文案拿了語音辨識的語言
MISUSE = re.compile(r"NextStrings\.Get\([^;]*?\bNextConfig\.Language\b")
#: 假名、CJK 統一漢字、全形標點與符號（含 `・` `（` `：`）
CJK = re.compile(r"[　-ヿ㐀-鿿！-｠]")
#: emoji 與 dingbats（Noto Sans JP／SC／TC 沒有這些字形；★ ♪ ◆ 這類 JIS 符號有，不擋）
EMOJI = re.compile("[\U0001F000-\U0001FFFF\u2700-\u27BF\uFE0F]")
LITERAL = re.compile(r'\$?@?"((?:[^"\\\n]|\\.)*)"')
PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
KEY = re.compile(r"^[a-z][a-z0-9_]*$")
CALL = re.compile(r"NextStrings\.(?:Get|F)\(")


def load_tables(folder: pathlib.Path = I18N) -> tuple[dict, list]:
    """`{locale: {key: text}}` 與錯誤清單。"""
    problems: list = []
    tables: dict = {}
    for locale in LOCALES:
        path = folder / f"next_{locale}.json"
        if not path.exists():
            problems.append(f"缺檔：{path.name}")
            continue
        duplicates: list = []

        def pairs(items, _dups=duplicates):
            seen: dict = {}
            for key, value in items:
                if key in seen:
                    _dups.append(key)
                seen[key] = value
            return seen

        try:
            data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
        except json.JSONDecodeError as error:
            problems.append(f"{path.name} 不是合法 JSON：{error}")
            continue
        for key in duplicates:
            problems.append(f"{path.name} 重複 key：{key}")
        for key, value in data.items():
            if not isinstance(value, str) or not value.strip():
                problems.append(f"{path.name} 的 {key} 不是非空字串")
            elif EMOJI.search(value):
                problems.append(f"{path.name} 的 {key} 有 emoji（字型畫不出來）：{value}")
            if not KEY.match(key):
                problems.append(f"{path.name} 的 key 不合格式（小寫＋底線）：{key}")
        tables[locale] = data
    return tables, problems


def parity(tables: dict) -> list:
    """三份 key 一致、同一條的佔位符一致。"""
    problems = []
    if not tables:
        return problems
    base_locale = LOCALES[0] if LOCALES[0] in tables else next(iter(tables))
    base = tables[base_locale]
    for locale, table in tables.items():
        if locale == base_locale:
            continue
        for key in sorted(set(base) - set(table)):
            problems.append(f"next_{locale}.json 少了 {key}")
        for key in sorted(set(table) - set(base)):
            problems.append(f"next_{locale}.json 多了 {key}（{base_locale} 沒有）")
        for key in sorted(set(base) & set(table)):
            want = set(PLACEHOLDER.findall(str(base[key])))
            got = set(PLACEHOLDER.findall(str(table[key])))
            if want != got:
                problems.append(f"{key} 的佔位符不一致：{base_locale}={sorted(want)} {locale}={sorted(got)}")
    return problems


def strip_logs(text: str) -> str:
    """把 `Debug.Log*(…);` 整句換成空白（多行的也算），其餘原樣。"""
    out = []
    at = 0
    for match in re.finditer(r"Debug\.Log\w*\(", text):
        if match.start() < at:
            continue
        end = text.find(");", match.end())
        end = len(text) if end < 0 else end + 2
        out.append(text[at:match.start()])
        out.append(re.sub(r"[^\n]", " ", text[match.start():end]))
        at = end
    out.append(text[at:])
    return "".join(out)


def hardcoded_text(sources: dict) -> list:
    """`{檔名: 原始碼}` → 字串常值裡有中日文的 `檔名:行號 行內容`（註解與 `Debug.Log` 不算）。"""
    hits = []
    for name, text in sorted(sources.items()):
        cleaned = strip_logs(str(text))
        original = str(text).splitlines()
        for number, line in enumerate(cleaned.splitlines(), 1):
            if line.strip().startswith("//"):
                continue
            code = line.split("//", 1)[0]
            if any(CJK.search(match.group(1)) or EMOJI.search(match.group(1)) for match in LITERAL.finditer(code)):
                hits.append(f"{name}:{number} {original[number - 1].strip()[:100]}")
    return hits


def _first_argument(text: str, start: int) -> str:
    """`NextStrings.Get(` 之後到第一個頂層逗號（或收尾括號）的那一段。"""
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return text[start:index]
            depth -= 1
        elif char == "," and depth == 0:
            return text[start:index]
    return text[start:]


def key_usage(sources: dict, table: dict) -> tuple[list, list]:
    """（程式用了但表裡沒有的 key, 表裡有但沒人用的 key）。"""
    referenced = set()
    literals = set()
    for text in sources.values():
        for match in LITERAL.finditer(str(text)):
            literals.add(match.group(1))
        for call in CALL.finditer(str(text)):
            argument = _first_argument(str(text), call.end())
            # 比較用的（`winner == "black" ? …`）、拼接用的前綴（`"bg_" + id`）、
            # 別的呼叫的參數（`row.OptBoolean("online") ? …`）都不是 key
            argument = re.sub(r'(==|!=)\s*"[^"]*"|"[^"]*"\s*(==|!=|\+)|\w\(\s*"[^"]*"\s*\)', " ", argument)
            for match in LITERAL.finditer(argument):
                if KEY.match(match.group(1)):
                    referenced.add(match.group(1))
    missing = sorted(key for key in referenced if key not in table)
    unused = sorted(key for key in table
                    if key not in literals and not key.startswith(DYNAMIC_PREFIXES))
    return missing, unused


def language_misuse(sources: dict) -> list:
    """`{檔名: 原始碼}` → 違規的 `檔名:行號 行內容`（註解行不算）。"""
    hits = []
    for name, text in sorted(sources.items()):
        for number, line in enumerate(str(text).splitlines(), 1):
            code = line.split("//", 1)[0]
            if MISUSE.search(code):
                hits.append(f"{name}:{number} {line.strip()[:90]}")
    return hits


def main() -> int:
    tables, problems = load_tables()
    problems += parity(tables)
    sources = {path.name: path.read_text(encoding="utf-8") for path in sorted([*NEXT.glob("*.cs"), *MR.glob("*.cs")])}
    for hit in hardcoded_text(sources):
        problems.append("寫死的中日文（要走 NextStrings）：" + hit)
    missing, unused = key_usage(sources, tables.get(LOCALES[0], {}))
    for key in missing:
        problems.append(f"程式用了表裡沒有的 key：{key}")
    for key in unused:
        problems.append(f"表裡沒人用的 key：{key}")
    for hit in language_misuse(sources):
        problems.append("界面文案用了語音辨識的語言（NextConfig.Language；要用 UiLanguage）：" + hit)
    if problems:
        print("✗ Next 界面文字不合格：")
        for problem in problems:
            print("   " + problem)
        return 1
    count = len(tables.get(LOCALES[0], {}))
    print(f"✓ Next 界面文字乾淨：{count} 條 × {len(LOCALES)} 語，key／佔位符一致，程式裡沒有寫死的中日文")
    return 0


if __name__ == "__main__":
    sys.exit(main())
