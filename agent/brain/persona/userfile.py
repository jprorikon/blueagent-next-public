"""每個 user 一份 `user.md`：**兩段**——`identity`（手寫，不被程式改）＋ `recent`（自動累積，有預算）。

owner 2026-10-22 定的結構：

    ## 权限        ← 委任的唯一開關（fail-closed，只讀檔＋token 角色）
    ## identity    ← 稱呼／關係／禁忌：**手寫**，程式只讀不寫
    ## recent      ← 自動累積的「最近」（去重、上限、超預算就整理）

為什麼要分兩段：`identity` 是「這個人是誰」，一句都不該被自動邏輯覆蓋；
`recent` 是「最近發生什麼」，可以自動長、也必須會自己收。

預算（owner：「限制在 4k token，超長對 recent 段整理」）：
  · 整份檔案 ≤ `TOKEN_BUDGET`（4000 token，估算見 `estimate_tokens`）。
  · 超過時對 **recent 段**做三件事：① 去重 ② 把最舊的幾條「落地」成記憶（callback）
    ③ 從檔案裡移除——所以 user.md 不會長成第二份記憶庫。

⚠️ 舊格式（`## 偏好` / `## 備註`）啟動時自動升級：偏好→recent、備註＋稱呼→identity。
"""

from __future__ import annotations

import math
import pathlib
import re
import threading

#: 整份 user.md 的 token 預算（owner 2026-10-22）。
TOKEN_BUDGET = 4000
#: recent 段最多幾條（硬上限，避免一次爆量）。
MAX_ITEMS = 40
#: 超預算時一次落地幾條（從最舊的開始）。
COMPACT_BATCH = 8

_HEADER = "# {name}（{principal}）"

#: 權限段的標題（日語）。舊檔是簡體的 `权限`，`ensure()` 會原地升級標題（值不動）。
PERMS = "権限"
_PERMS_LEGACY = "权限"                           # lang-ok: 舊檔標題（升級用）
#: 委任放行的值：新寫法 `許可`，舊檔的 `允许`／英文照認（fail-closed：其餘一律不放）
_ALLOW_VALUES = {"許可", "允许", "allow", "yes", "true", "on", "1"}   # lang-ok: 舊檔的值照認
#: recent 空的時候的佔位（舊檔是 `（还没有）`）
_EMPTY_RECENT = "（まだない）"
_EMPTY_RECENT_LEGACY = "（还没有）"               # lang-ok: 舊檔佔位（升級用）
_PERMS_NOTE = "（この行だけが委任のスイッチ。コードはこれと token の役割の両方を見て、両方通ったときだけ許可する。）"
_PERMS_NOTE_LEGACY = "（这一行是**唯一**的委任开关；代码判它＋token 角色，两者都通过才放行。）"  # lang-ok: 舊檔升級用的比對字串

_DEFAULT = """{header}

## 権限
委任：{delegate}
""" + _PERMS_NOTE + """

## identity
{identity}

## recent
""" + _EMPTY_RECENT + "\n"


def estimate_tokens(text: str) -> int:
    """粗估 token 數（不需要精確，只要單調）。

    CJK／假名一字 ≈ 1 token，ASCII 一個詞 ≈ 1.3，其餘（標點、空白）≈ 0.3。
    寧可高估：預算是護欄，不是省錢工具。
    """
    body = str(text or "")
    cjk = sum(1 for char in body if "\u3040" <= char <= "\u9fff" or "\uac00" <= char <= "\ud7af")
    ascii_words = len(re.findall(r"[A-Za-z0-9_]+", body))
    other = max(0, len(body) - cjk - sum(len(word) for word in re.findall(r"[A-Za-z0-9_]+", body)))
    return int(math.ceil(cjk + ascii_words * 1.3 + other * 0.3))


def _slug(principal: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(principal or "").strip()).strip("-")
    return clean or "unknown"


class UserFile:
    """一個人的那一頁。`read()`／`remember()`／`compact()` 都是就地改檔。"""

    def __init__(self, root, *, host: str = "owner", token_budget: int = TOKEN_BUDGET,
                 on_overflow=None) -> None:
        self.root = pathlib.Path(root)
        self.host = str(host or "owner")
        self.token_budget = int(token_budget)
        #: 超預算時的落地 hook：`on_overflow(principal, items)`（通常接記憶）
        self.on_overflow = on_overflow
        self._lock = threading.RLock()

    # ── 位置 ────────────────────────────────────────────────────────────────
    def path_for(self, principal: str) -> pathlib.Path:
        return self.root / f"{_slug(principal)}.md"

    def exists(self, principal: str) -> bool:
        return self.path_for(principal).is_file()

    # ── 建立／升級 ──────────────────────────────────────────────────────────
    def ensure(self, principal: str, *, name: str = "", call_name: str = "") -> pathlib.Path:
        path = self.path_for(principal)
        with self._lock:
            if path.is_file():
                self._migrate(principal, path)
                self._upgrade_language(path)
                return path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_DEFAULT.format(
                header=_HEADER.format(name=name or principal, principal=principal),
                delegate="許可" if str(principal) == self.host else "不可",
                identity=(call_name or "<手書き欄：相手が誰か・呼び方・触れてほしくないこと。"
                                    "ここはプログラムが書き換えない。>")),
                encoding="utf-8")
        return path

    def _migrate(self, principal: str, path: pathlib.Path) -> None:
        """舊格式（偏好／備註）→ 新格式（identity／recent）。只做一次。"""
        text = path.read_text(encoding="utf-8")
        if "## recent" in text and "## identity" in text:
            return
        call_name = _section(text, "称呼").strip()
        prefs = _section(text, "偏好").strip()
        notes = _section(text, "备注").strip() or _section(text, "備註").strip()
        permissions = _section(text, _PERMS_LEGACY).strip() or (
            "委任：許可" if str(principal) == self.host else "委任：不可")
        identity = "\n".join(row for row in (call_name, notes) if row).strip() or "（まだ書かれていない）"
        recent = prefs or "（まだない）"
        rebuilt = (_HEADER.format(name=principal, principal=principal) + "\n\n"
                   f"## {PERMS}\n" + permissions + "\n\n"
                   "## identity\n" + identity + "\n\n"
                   "## recent\n" + recent + "\n")
        path.write_text(rebuilt, encoding="utf-8")

    def _upgrade_language(self, path: pathlib.Path) -> None:
        """舊檔的中文骨架 → 日語（review 2026-10-28：這一頁每輪都進 prompt）。

        只換**骨架**：權限段標題、那行說明、recent 的空佔位。委任的值（`允许`）原樣留著，
        `allows()` 照認——值是開關本身，不在這裡替他改。
        """
        text = path.read_text(encoding="utf-8")
        upgraded = re.sub(rf"^##\s*{_PERMS_LEGACY}[^\n]*$", f"## {PERMS}", text, count=1,
                          flags=re.MULTILINE)
        upgraded = upgraded.replace(_PERMS_NOTE_LEGACY, _PERMS_NOTE)
        upgraded = upgraded.replace(_EMPTY_RECENT_LEGACY, _EMPTY_RECENT)
        if upgraded != text:
            path.write_text(upgraded, encoding="utf-8")

    # ── 讀 ──────────────────────────────────────────────────────────────────
    def read(self, principal: str) -> str:
        try:
            return self.path_for(principal).read_text(encoding="utf-8")
        except OSError:
            return ""

    def block(self, principal: str, *, name: str = "") -> str:
        """貼進 prompt 的那一段（每一輪都進，所以要短——預算在這裡守）。"""
        fixed, recent = self.block_parts(principal, name=name)
        body = "\n\n".join(part for part in (fixed, recent) if part.strip())
        if not body:
            return ""
        return ("### この人について（毎回かならず読む・推測で埋めない）\n"
                f"{body}")

    def block_parts(self, principal: str, *, name: str = "") -> tuple[str, str]:
        """把這一頁拆成 **固定段**（稱呼／權限／identity，程式不改）與 **動態段**（recent）。

        為什麼要拆（owner 2026-10-22 第 ③ 步）：固定段的字串要逐字穩定，
        這樣才能算 hash 做觀測、也才能吃到 provider 的 prefix cache；
        `recent` 每輪都可能變，不能混進固定段。
        """
        path = self.ensure(principal, name=name)
        text = path.read_text(encoding="utf-8")
        self._compact_if_needed(principal, text)
        body = path.read_text(encoding="utf-8").strip()
        if not body:
            return "", ""
        recent_body = _section(body, "recent").strip()
        # 權限段**照解析結果重畫**（不是原文照貼）：值一律寫成「許可／不可」，
        # 舊檔手寫的 `允许`／說明行不會把中文帶進 prompt。
        permissions = "\n".join(f"{key}：{'許可' if _allowed(value) else '不可'}"
                                for key, value in self.permissions(principal).items())
        identity = _section(body, "identity").strip()
        head = _HEADER.format(name=name or principal, principal=principal)
        fixed = "\n\n".join(part for part in (
            head, (f"## {PERMS}\n" + permissions) if permissions else "",
            ("## identity\n" + identity) if identity else "") if part)
        recent = ("## recent\n" + recent_body) if recent_body else ""
        return fixed, recent

    def recent(self, principal: str) -> list[str]:
        return [line[2:].strip() for line in _section(self.read(principal), "recent").splitlines()
                if line.strip().startswith("- ")]

    def preferences(self, principal: str) -> list[str]:
        """舊名相容：以前叫「偏好」，現在叫「recent」（同一段）。"""
        return self.recent(principal)

    def identity(self, principal: str) -> str:
        return _section(self.read(principal), "identity").strip()

    def call_name(self, principal: str) -> str:
        """identity 段第一行通常就是稱呼；抓不到就整段當沒有。"""
        identity = self.identity(principal)
        first = identity.splitlines()[0].strip() if identity else ""
        return "" if first.startswith("（") else first

    # ── 寫 ──────────────────────────────────────────────────────────────────
    def remember(self, principal: str, item: str, *, name: str = "",
                 replace_prefix: str = "") -> bool:
        """往 recent 加一條。

        `replace_prefix`：這個前綴開頭的舊條目先刪掉（同一主體＋同一謂詞只留最新一條，
        例如「主人 喜欢…」只會有一條）——這是 ④「不要把 recent 變成對話副本」的另一半：
        穩定事實要收斂，不是愈積愈多。
        """
        item = str(item or "").strip().lstrip("-").strip()
        if not item:
            return False
        path = self.ensure(principal, name=name)
        with self._lock:
            current = self.recent(principal)
            if replace_prefix:
                current = [row for row in current
                           if not row.lstrip("- ").startswith(str(replace_prefix))]
            if any(_norm(row) == _norm(item) for row in current):   # 去重
                return False
            rows = current + [item]
            if len(rows) > MAX_ITEMS:
                self._land(principal, rows[:len(rows) - MAX_ITEMS])
                rows = rows[-MAX_ITEMS:]
            self._write_recent(path, principal, rows)
            self._compact_if_needed(principal, path.read_text(encoding="utf-8"))
        return True

    def _write_recent(self, path: pathlib.Path, principal: str, rows: list[str]) -> None:
        body = "\n".join(f"- {row}" for row in rows) if rows else _EMPTY_RECENT
        text = _replace_section(path.read_text(encoding="utf-8"), "recent", body)
        path.write_text(text, encoding="utf-8")

    # ── 預算與整理 ──────────────────────────────────────────────────────────
    def _compact_if_needed(self, principal: str, text: str) -> None:
        """超過 token 預算 ⇒ 把 recent 最舊的幾條落地成記憶、再從檔案移除。"""
        if estimate_tokens(text) <= self.token_budget:
            return
        rows = self.recent(principal)
        if not rows:
            return
        drop = min(COMPACT_BATCH, max(1, len(rows) // 2))
        self._land(principal, rows[:drop])
        self._write_recent(self.path_for(principal), principal, rows[drop:])

    def _land(self, principal: str, items: list[str]) -> None:
        """把 recent 的幾條交給記憶層（callback 沒接就只丟棄——不假裝寫成功）。"""
        if not items:
            return
        if self.on_overflow is None:
            return
        try:
            self.on_overflow(str(principal), list(items))
        except Exception:                     # noqa: BLE001 - 落地失敗不能影響對話
            pass

    # ── 權限（委任的唯一開關）─────────────────────────────────────────────────
    def permissions(self, principal: str) -> dict[str, str]:
        out: dict[str, str] = {}
        text = self.read(principal)
        for line in (_section(text, PERMS) or _section(text, _PERMS_LEGACY)).splitlines():
            if not line.strip().startswith((("-", "*"))):
                if "：" in line:
                    key, _, value = line.partition("：")
                elif ":" in line:
                    key, _, value = line.partition(":")
                else:
                    continue
                out[key.strip()] = value.strip()
        return out

    def allows(self, principal: str, capability: str = "委任") -> bool:
        """fail-closed：沒寫、寫錯、格式不對 ⇒ False（還要 token 那一側的角色才算數）。"""
        return _allowed(self.permissions(principal).get(capability, ""))

    def set_permission(self, principal: str, capability: str, value: str, *,
                       name: str = "") -> bool:
        path = self.ensure(principal, name=name)
        with self._lock:
            rows = self.permissions(principal)
            rows[capability] = str(value).strip()
            body = "\n".join(f"{key}：{val}" for key, val in rows.items())
            path.write_text(_replace_section(path.read_text(encoding="utf-8"), PERMS, body),
                            encoding="utf-8")
        return True

    def set_call_name(self, principal: str, call_name: str, *, name: str = "") -> bool:
        """改稱呼＝改 identity 段的第一行（其餘手寫內容原樣保留）。"""
        call_name = str(call_name or "").strip()
        if not call_name:
            return False
        path = self.ensure(principal, name=name)
        with self._lock:
            current = self.identity(principal)
            rest = current.splitlines()[1:]
            body = "\n".join([call_name, *rest]).strip()
            path.write_text(_replace_section(path.read_text(encoding="utf-8"), "identity", body),
                            encoding="utf-8")
        return True

    def set_identity(self, principal: str, body: str, *, name: str = "") -> bool:
        path = self.ensure(principal, name=name)
        with self._lock:
            path.write_text(_replace_section(path.read_text(encoding="utf-8"), "identity",
                                             str(body or "").strip() or "（まだ書かれていない）"),
                            encoding="utf-8")
        return True

    # ── 收 ──────────────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        rows = []
        for path in sorted(self.root.glob("*.md")) if self.root.exists() else []:
            text = path.read_text(encoding="utf-8", errors="replace")
            rows.append({"principal": path.stem, "bytes": path.stat().st_size,
                         "tokens": estimate_tokens(text), "path": str(path)})
        return {"users": rows, "host": self.host, "token_budget": self.token_budget,
                "max_items": MAX_ITEMS}


def facts_to_remember(facts, principal: str) -> list[str]:
    """從一輪的事實裡挑「**他自己說的**」那幾條，準備寫進他的 recent 段。

    只吃 `explicit`／`rule` 這兩種來源：她自己觀察到的、推測的、別人的，
    都不該被寫成「這個人的偏好」（那會變成她替對方捏造人格）。
    """
    out: list[str] = []
    for fact in facts or ():
        source = str(getattr(fact, "source", "") or "")
        if source not in {"explicit", "rule"}:
            continue
        if str(getattr(fact, "principal", "") or "") != str(principal):
            continue
        text = str(getattr(fact, "text", "") or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _norm(item: str) -> str:
    return re.sub(r"\s+", "", str(item or "")).lower()


def _allowed(value) -> bool:
    return str(value or "").strip().lower() in _ALLOW_VALUES


def _section(text: str, title: str) -> str:
    """抓 `## <title>` 到下一個 `## ` 之間。"""
    # 標題後面允許附註（例如舊檔的「## 偏好（自动累积，最多 12 条）」）
    pattern = re.compile(rf"^##\s*{re.escape(title)}[^\n]*\n(.*?)(?=^##\s|\Z)",
                         re.MULTILINE | re.DOTALL)
    found = pattern.search(text or "")
    return found.group(1).strip("\n") if found else ""


def _replace_section(text: str, title: str, body: str) -> str:
    pattern = re.compile(rf"^##\s*{re.escape(title)}[^\n]*\n(.*?)(?=^##\s|\Z)",
                         re.MULTILINE | re.DOTALL)
    block = f"## {title}\n{body}\n"
    if pattern.search(text or ""):
        return pattern.sub(lambda _m: block + "\n", text, count=1).rstrip() + "\n"
    return (text.rstrip() + "\n\n" + block).strip() + "\n"


__all__ = ["COMPACT_BATCH", "MAX_ITEMS", "TOKEN_BUDGET", "UserFile", "estimate_tokens",
           "facts_to_remember"]
