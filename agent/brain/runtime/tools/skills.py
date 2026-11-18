"""`skill.view`：取一支工作技能的全文。"""

from __future__ import annotations

from agent.brain.runtime.tools.base import ToolContext, ToolResult


class SkillViewTool:
    """`skill.view`：取一支工作技能的全文（索引在 prompt 裡，正文按需取）。

    權限跟索引同一條規則：`scope`（chat／work／both）與 `owner_only`。
    """

    name = "skill.view"
    schema = {
        "type": "function",
        "function": {
            "name": "skill_view",
            "description": (
                "**仕事スキル**の全文（手順／完了条件／失敗したとき）を取り出す。"
                "システムプロンプトにはスキル一覧と一行説明しかない。手順どおりにやる前に全文を取る。"
                "技能名が不確かなら、まずヒントの一覧を見る。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "スキル名（例：weather）"},
                },
                "required": ["name"],
            },
        },
    }

    def __init__(self, skills) -> None:
        self.skills = skills

    def run(self, ctx, arguments: dict) -> "ToolResult":
        args = dict(arguments or {})
        name = str(args.get("name") or "").strip()
        if not name:
            return ToolResult(ok=False, error="empty_name")
        if self.skills is None:
            return ToolResult(ok=False, error="skills_unavailable")
        skill, reason = self.skills.view(name, mode=str(ctx.mode or "chat"),
                                         is_owner=bool(ctx.is_owner), entity=str(ctx.entity))
        if skill is None:
            return ToolResult(ok=False, error=reason or "not_found")
        if reason:
            return ToolResult(ok=False, error=reason)
        return ToolResult(ok=True, data={"name": skill.name, "scope": skill.scope,
                                         "requires": list(skill.requires),
                                         "body": skill.body})


__all__ = ["SkillViewTool"]
