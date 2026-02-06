from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from chordcode.skills import SkillLoader


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


class SkillLoaderTests(unittest.TestCase):
    def test_discovers_skills_from_all_supported_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            cwd = worktree / "nested" / "app"
            cwd.mkdir(parents=True, exist_ok=True)

            _write_skill(worktree, "skills/alpha", "alpha", "alpha desc")
            _write_skill(worktree, ".claude/skills/bravo", "bravo", "bravo desc")
            _write_skill(worktree, ".agents/skills/charlie", "charlie", "charlie desc")
            _write_skill(worktree, ".opencode/skill/delta", "delta", "delta desc")
            _write_skill(worktree, ".opencode/skills/echo", "echo", "echo desc")

            loader = SkillLoader(worktree=str(worktree), cwd=str(cwd))
            names = {s.name for s in loader.list_skills()}

            self.assertIn("alpha", names)
            self.assertIn("bravo", names)
            self.assertIn("charlie", names)
            self.assertIn("delta", names)
            self.assertIn("echo", names)

    def test_skips_missing_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            cwd = worktree
            bad = worktree / "skills" / "bad"
            bad.mkdir(parents=True, exist_ok=True)
            (bad / "SKILL.md").write_text("# no frontmatter", encoding="utf-8")

            loader = SkillLoader(worktree=str(worktree), cwd=str(cwd))
            self.assertEqual(loader.list_skills(), [])

    def test_skips_name_directory_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            cwd = worktree
            _write_skill(worktree, "skills/folder-name", "different-name", "desc")

            loader = SkillLoader(worktree=str(worktree), cwd=str(cwd))
            self.assertEqual(loader.list_skills(), [])

    def test_skips_invalid_name_regex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            cwd = worktree
            _write_skill(worktree, "skills/bad_name", "bad_name", "desc")

            loader = SkillLoader(worktree=str(worktree), cwd=str(cwd))
            self.assertEqual(loader.list_skills(), [])

    def test_duplicate_prefers_nearer_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            near = worktree / "a" / "b"
            near.mkdir(parents=True, exist_ok=True)

            _write_skill(worktree, "skills/dup-skill", "dup-skill", "far")
            _write_skill(near, "skills/dup-skill", "dup-skill", "near")

            loader = SkillLoader(worktree=str(worktree), cwd=str(near))
            dup = loader.get("dup-skill")
            self.assertIsNotNone(dup)
            self.assertEqual(dup.description, "near")
            self.assertIn("/a/b/skills/dup-skill/SKILL.md", dup.path)

    def test_skips_empty_or_too_long_description(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            cwd = worktree
            _write_skill(worktree, "skills/empty-desc", "empty-desc", "")
            _write_skill(worktree, "skills/long-desc", "long-desc", "x" * 1025)

            loader = SkillLoader(worktree=str(worktree), cwd=str(cwd))
            self.assertEqual(loader.list_skills(), [])


if __name__ == "__main__":
    unittest.main()
