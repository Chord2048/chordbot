from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Any

from chordcode.model import PermissionRule
from chordcode.permission.rules import evaluate_permission
from chordcode.skills import SkillLoader
from chordcode.tools.base import ToolResult


@dataclass(frozen=True)
class SkillCtx:
    worktree: str
    cwd: str
    permission_rules: list[PermissionRule]


class SkillTool:
    name = "skill"

    def __init__(self, ctx: SkillCtx) -> None:
        self._ctx = ctx
        self._loader = SkillLoader(worktree=ctx.worktree, cwd=ctx.cwd)
        self._visible_skills = [
            s for s in self._loader.list_skills() if evaluate_permission("skill", s.name, self._ctx.permission_rules).action != "deny"
        ]
        self.description = self._build_description()

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name from available_skills"},
            },
            "required": ["name"],
        }

    async def execute(self, args: dict[str, Any], ctx) -> ToolResult:
        name = str(args.get("name", "")).strip()
        if not name:
            raise ValueError("name is required")

        skill = self._loader.get(name)
        if not skill:
            available = ", ".join(s.name for s in self._visible_skills) or "none"
            return ToolResult(
                title="Skill not found",
                output=f'Skill "{name}" not found. Available skills: {available}',
                metadata={"error": True, "name": name},
            )

        if evaluate_permission("skill", skill.name, self._ctx.permission_rules).action == "deny":
            available = ", ".join(s.name for s in self._visible_skills) or "none"
            return ToolResult(
                title="Skill access denied",
                output=f'Skill "{name}" is not available in the current permission rules. Available skills: {available}',
                metadata={"error": True, "name": name},
            )

        await ctx.ask(
            permission="skill",
            patterns=[skill.name],
            always=[skill.name],
            metadata={"skill_path": skill.path},
        )

        files = self._loader.sample_files(skill.name)
        file_lines = "\n".join([f"<file>{escape(p)}</file>" for p in files])

        output = "\n".join(
            [
                f'<skill_content name="{escape(skill.name)}">',
                f"# Skill: {skill.name}",
                "",
                skill.body,
                "",
                f"Base directory for this skill: {skill.dir}",
                "Relative paths in this skill are resolved from this base directory.",
                "",
                "<skill_files>",
                file_lines,
                "</skill_files>",
                "</skill_content>",
            ]
        )

        return ToolResult(
            title=f"Loaded skill: {skill.name}",
            output=output,
            metadata={"name": skill.name, "dir": skill.dir, "path": skill.path},
        )

    def _build_description(self) -> str:
        if not self._visible_skills:
            return (
                "Load a specialized skill that provides domain-specific instructions and workflows. "
                "No skills are currently available."
            )

        lines = [
            "Load a specialized skill that provides domain-specific instructions and workflows.",
            "",
            "When a task matches one of the skills below, load it with this tool.",
            "",
            "<available_skills>",
        ]
        for skill in self._visible_skills:
            lines.extend(
                [
                    "  <skill>",
                    f"    <name>{escape(skill.name)}</name>",
                    f"    <description>{escape(skill.description)}</description>",
                    f"    <location>{escape(skill.path)}</location>",
                    "  </skill>",
                ]
            )
        lines.extend(["</available_skills>"])
        return "\n".join(lines)
