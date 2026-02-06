from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from chordcode.model import PermissionRule
from chordcode.tools.skill import SkillCtx, SkillTool


def _write_skill(root: Path, folder: str, name: str, description: str, body: str = "# Body\n") -> Path:
    skill_dir = root / folder
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "\n".join(
            [
                "---",
                f"name: {name}",
                f"description: {description}",
                "---",
                "",
                body,
            ]
        ),
        encoding="utf-8",
    )
    return skill_md


@dataclass
class FakeToolCtx:
    session_id: str = "s1"
    message_id: str = "m1"
    agent: str = "primary"
    asks: list[dict] = field(default_factory=list)

    async def ask(self, *, permission: str, patterns: list[str], always: list[str], metadata: dict) -> None:
        self.asks.append(
            {
                "permission": permission,
                "patterns": patterns,
                "always": always,
                "metadata": metadata,
            }
        )

    async def tool_stream_update(self, output: str) -> None:
        _ = output


class SkillToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_description_contains_available_skills_and_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = _write_skill(root, "skills/demo-skill", "demo-skill", "Demo skill")

            tool = SkillTool(
                SkillCtx(
                    worktree=str(root),
                    cwd=str(root),
                    permission_rules=[PermissionRule(permission="*", pattern="*", action="allow")],
                )
            )

            self.assertIn("<available_skills>", tool.description)
            self.assertIn("<name>demo-skill</name>", tool.description)
            self.assertIn(f"<location>{path.resolve()}</location>", tool.description)

    async def test_description_hides_denied_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skill(root, "skills/open-skill", "open-skill", "Open skill")
            _write_skill(root, "skills/hidden-skill", "hidden-skill", "Hidden skill")

            tool = SkillTool(
                SkillCtx(
                    worktree=str(root),
                    cwd=str(root),
                    permission_rules=[
                        PermissionRule(permission="skill", pattern="hidden-skill", action="deny"),
                        PermissionRule(permission="*", pattern="*", action="allow"),
                    ],
                )
            )

            self.assertIn("<name>open-skill</name>", tool.description)
            self.assertNotIn("<name>hidden-skill</name>", tool.description)

    async def test_execute_requests_skill_permission_and_returns_structured_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_md = _write_skill(root, "skills/demo-skill", "demo-skill", "Demo skill", "## Instructions\nUse it.\n")
            scripts_dir = skill_md.parent / "scripts"
            scripts_dir.mkdir(parents=True, exist_ok=True)
            helper = scripts_dir / "helper.sh"
            helper.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

            tool = SkillTool(
                SkillCtx(
                    worktree=str(root),
                    cwd=str(root),
                    permission_rules=[PermissionRule(permission="*", pattern="*", action="allow")],
                )
            )
            ctx = FakeToolCtx()

            out = await tool.execute({"name": "demo-skill"}, ctx)

            self.assertEqual(len(ctx.asks), 1)
            self.assertEqual(ctx.asks[0]["permission"], "skill")
            self.assertEqual(ctx.asks[0]["patterns"], ["demo-skill"])
            self.assertEqual(ctx.asks[0]["always"], ["demo-skill"])

            self.assertIn('<skill_content name="demo-skill">', out.output)
            self.assertIn(f"Base directory for this skill: {skill_md.parent.resolve()}", out.output)
            self.assertIn("<skill_files>", out.output)
            self.assertIn(f"<file>{helper.resolve()}</file>", out.output)

    async def test_execute_returns_readable_error_for_unknown_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_skill(root, "skills/known-skill", "known-skill", "Known skill")
            tool = SkillTool(
                SkillCtx(
                    worktree=str(root),
                    cwd=str(root),
                    permission_rules=[PermissionRule(permission="*", pattern="*", action="allow")],
                )
            )
            ctx = FakeToolCtx()

            out = await tool.execute({"name": "unknown-skill"}, ctx)
            self.assertEqual(len(ctx.asks), 0)
            self.assertEqual(out.title, "Skill not found")
            self.assertIn('Skill "unknown-skill" not found', out.output)
            self.assertIn("known-skill", out.output)


if __name__ == "__main__":
    unittest.main()
