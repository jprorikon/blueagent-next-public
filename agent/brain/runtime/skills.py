"""工作技能（skills）：**人格不進這裡**，只放「一件事怎麼做」。

owner 2026-10-23 定的口徑：

  · 人格（她是誰、怎麼說話）住 `agent/persona/` 與 `persona/*.core.json`，**不寫成 skill**。
  · skill 只放工作技能；其中有一類**聊天也要能用**（查天氣、查資料、翻譯），其餘給委任任務。
  · 權限隔離只用在「能不能委任 Hermes」（`owner_only`），不是整個技能庫鎖 owner。

一支技能就是一個資料夾：

    brain/skills/<name>/SKILL.md
    ---
    name: weather
    description: 聊天裡被問天氣時用
    scope: chat            # chat（任何模式都能用）｜work（只在委任模式）｜both
    requires: [tool:weather.get]
    owner_only: false
    ---
    正文（什麼情況用、步驟、驗收、失敗怎麼辦）

兩層來源（後者覆寫前者，給角色專用版）：
    brain/skills/<name>/SKILL.md
    data/skills/<entity>/<name>.md
"""

from __future__ import annotations

import hashlib
import pathlib
import re

SCOPES = ("chat", "work", "both")


def _parse_front(text: str) -> tuple[dict, str]:
    """極簡 frontmatter：`---` 之間是 key: value，其餘是正文。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    head, body = parts[1], parts[2]
    meta: dict = {}
    for line in head.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [item.strip().strip("'\"") for item in value[1:-1].split(",")
                          if item.strip()]
        elif value.lower() in {"true", "false"}:
            meta[key] = value.lower() == "true"
        else:
            meta[key] = value.strip("'\"")
    return meta, body.lstrip("\n")


class Skill:
    def __init__(self, *, name: str, description: str, scope: str, body: str,
                 requires=(), owner_only: bool = False, path: str = "", source: str = "",
                 status: str = "stable", trial: str = "", learned_from: str = "",
                 category: str = "") -> None:
        self.name = str(name)
        self.description = str(description or "").strip()
        self.scope = scope if scope in SCOPES else "work"
        self.body = str(body or "")
        self.requires = tuple(str(item) for item in (requires or ()))
        self.owner_only = bool(owner_only)
        self.path = str(path)
        self.source = str(source)
        # 學習型技能的三個欄位（owner 2026-11-03）：trial ⇒ 成功 2 次才 stable；retired 不進索引
        self.status = str(status or "stable").strip().lower()
        self.trial = str(trial or "").strip()
        self.learned_from = str(learned_from or "").strip()
        #: 樹狀索引的分支（owner 2026-11-03：不必每次把所有技能正文都塞進 prompt）
        self.category = str(category or "").strip().lower() or "general"

    def usable(self, *, mode: str = "chat", is_owner: bool = True) -> bool:
        """`chat` ＝**任何模式都能用**（聊天是最小的上下文，能用的地方更多）；
        `work` ＝只在委任模式；`both` ＝兩邊都算。"""
        if self.owner_only and not is_owner:
            return False
        mode = str(mode or "chat").strip().lower()
        if self.scope == "chat":
            return True
        return self.scope in ("both", mode)

    def tools(self) -> tuple[str, ...]:
        """`requires` 裡的工具名（`tool:weather.get` → `weather.get`）。"""
        return tuple(item[len("tool:"):].strip() for item in self.requires
                     if item.startswith("tool:") and item[len("tool:"):].strip())

    def reachable(self, tool_available=None) -> bool:
        """這個渠道能不能用：不需要工具的技能永遠可以；需要工具的，至少要有一支在這個渠道可用。"""
        names = self.tools()
        if not names or tool_available is None:
            return True
        return any(tool_available(name) for name in names)

    def line(self) -> str:
        tag = "（オーナー専用）" if self.owner_only else ""
        mark = "" if self.status == "stable" else f"（{self.status}）"
        return f"- {self.name}：{self.description}［{self.scope}］{mark}{tag}"

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "scope": self.scope,
                "requires": list(self.requires), "owner_only": self.owner_only,
                "path": self.path, "source": self.source, "chars": len(self.body)}


class SkillLibrary:
    def __init__(self, root=None, *, entity_root=None, entity: str = "",
                 max_index: int = 24) -> None:
        self.root = pathlib.Path(root) if root is not None else None
        self.entity_root = pathlib.Path(entity_root) if entity_root is not None else None
        self.entity = str(entity or "")
        self.max_index = max(1, int(max_index))
        self._cache: dict[str, list[Skill]] = {}
        self._cache_stamp: dict[str, float] = {}

    # ── 讀 ──────────────────────────────────────────────────────────────────
    def _load_package(self) -> list[Skill]:
        out: list[Skill] = []
        if self.root is None or not self.root.exists():
            return out
        for path in sorted(self.root.glob("*/SKILL.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            meta, body = _parse_front(text)
            out.append(Skill(name=str(meta.get("name") or path.parent.name),
                             description=str(meta.get("description") or ""),
                             scope=str(meta.get("scope") or "work"),
                             body=body, requires=meta.get("requires") or (),
                             owner_only=bool(meta.get("owner_only") or False),
                             path=str(path), source="package",
                             status=str(meta.get("status") or "stable"),
                             trial=str(meta.get("trial") or ""),
                             learned_from=str(meta.get("learned_from") or ""),
                             category=str(meta.get("category") or "")))
        return out

    def _load_entity(self, entity: str) -> list[Skill]:
        out: list[Skill] = []
        if self.entity_root is None or not entity:
            return out
        folder = self.entity_root / entity
        if not folder.exists():
            return out
        for path in sorted(folder.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            meta, body = _parse_front(text)
            out.append(Skill(name=str(meta.get("name") or path.stem),
                             description=str(meta.get("description") or ""),
                             scope=str(meta.get("scope") or "work"),
                             body=body, requires=meta.get("requires") or (),
                             owner_only=bool(meta.get("owner_only") or False),
                             path=str(path), source="entity",
                             status=str(meta.get("status") or "stable"),
                             trial=str(meta.get("trial") or ""),
                             learned_from=str(meta.get("learned_from") or ""),
                             category=str(meta.get("category") or "")))
        return out

    def _stamp(self, entity: str) -> str:
        """目錄指紋：檔名＋奈秒 mtime＋大小。

        為什麼不用目錄 mtime：她剛寫完、快取又在同一秒建好時，秒級解析度分不出來
        （實測會拿到舊索引）。用「檔案清單的內容」當指紋就沒有這個問題。
        """
        rows = []
        for root in (self.root, self.entity_root / entity if (self.entity_root and entity) else None):
            if root is None:
                continue
            try:
                if not root.exists():
                    continue
                for path in sorted(root.glob("*.md")) + sorted(root.glob("*/SKILL.md")):
                    stat = path.stat()
                    rows.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")
            except OSError:
                continue
        return hashlib.sha1("|".join(rows).encode("utf-8")).hexdigest()

    def all(self, entity: str = "") -> list[Skill]:
        """角色專用版覆寫同名套件版。"""
        entity = str(entity or self.entity or "")
        key = entity or "-"
        cached = self._cache.get(key)
        if cached is not None and self._cache_stamp.get(key) == self._stamp(entity):
            return cached
        merged: dict[str, Skill] = {}
        for skill in self._load_package():
            merged[skill.name] = skill
        for skill in self._load_entity(entity):
            merged[skill.name] = skill
        skills = sorted(merged.values(), key=lambda item: item.name)
        self._cache[key] = skills
        self._cache_stamp[key] = self._stamp(entity)
        return skills

    def refresh(self) -> None:
        self._cache.clear()
        self._cache_stamp.clear()

    # ── 給 prompt / 工具用 ───────────────────────────────────────────────────
    def index(self, *, mode: str = "chat", is_owner: bool = True,
              entity: str = "", tool_available=None) -> list[Skill]:
        """`tool_available(name) -> bool`：這個渠道能不能用某支工具（`ToolRegistry.available`）；
        技能要的工具在這個渠道一支都用不了 ⇒ 不列（通話裡不列查網頁、TRPG 桌上不列天氣……）。"""
        rows = [skill for skill in self.all(entity)
                if skill.status != "retired"          # 退役的不再出現在索引（檔案還在，可救回）
                and skill.usable(mode=mode, is_owner=is_owner)
                and skill.reachable(tool_available)]
        return rows[: self.max_index]

    def render_index(self, *, mode: str = "chat", is_owner: bool = True,
                     entity: str = "", tool_available=None, per_group: int = 4) -> str:
        """樹狀索引（owner 2026-11-03）：按 `category` 分組，每組只列前 `per_group` 條。

        為什麼要這樣：技能一多，把所有正文塞進 prompt 不現實；連「一行一條」也會吃掉
        幾千字。現在只給**分支 + 代表作**，要看細節用 `skills_list(category=…)` 展開、
        要正文用 `skill.view(name)` 取——三層按需，跟檔案樹一樣。
        """
        rows = self.index(mode=mode, is_owner=is_owner, entity=entity, tool_available=tool_available)
        if not rows:
            return ""
        groups: dict[str, list[Skill]] = {}
        for skill in rows:
            groups.setdefault(skill.category, []).append(skill)
        lines: list[str] = []
        for category in sorted(groups):
            items = groups[category]
            shown = items[: max(1, int(per_group))]
            names = "、".join(skill.name for skill in shown)
            more = f"　ほか {len(items) - len(shown)} 件" if len(items) > len(shown) else ""
            lines.append(f"- [{category}] {len(items)} 件：{names}{more}")
        # ⚠️「先に skill.view、そのあと道具」は残す：按需解鎖的工具要讀過技能才出現，
        #    沒寫明的話她會以為「沒有那支工具」而放棄（舊版就是為這個寫的，樹狀化時一度掉了）
        return ("### あなたが使える技能（**目次だけ**。使うときは**先に** `skill.view(name)` で全文を取る"
                "（読むとその技能の道具が使えるようになる）。枝を広げるときは `skills_list(category=…)`）\n"
                + "\n".join(lines))

    def get(self, name: str, *, entity: str = "") -> Skill | None:
        wanted = str(name or "").strip().lower()
        for skill in self.all(entity):
            if skill.name.lower() == wanted:
                return skill
        return None

    def view(self, name: str, *, mode: str = "chat", is_owner: bool = True,
             entity: str = "") -> tuple[Skill | None, str]:
        skill = self.get(name, entity=entity)
        if skill is None:
            return None, "not_found"
        if not skill.usable(mode=mode, is_owner=is_owner):
            return skill, "not_allowed"
        return skill, ""


def _strip_code(text: str) -> str:
    return re.sub(r"`([^`]*)`", r"\1", text)
