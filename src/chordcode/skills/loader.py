from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from chordcode.log import log
from chordcode.tools.paths import is_within


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    path: str
    dir: str
    body: str
    raw_frontmatter: str


class SkillLoader:
    SEARCH_PATTERNS: tuple[str, ...] = (
        "skills/*/SKILL.md",
        ".claude/skills/*/SKILL.md",
        ".agents/skills/*/SKILL.md",
        ".opencode/skill/*/SKILL.md",
        ".opencode/skills/*/SKILL.md",
    )

    def __init__(self, *, worktree: str, cwd: str, sample_limit: int = 20) -> None:
        self._worktree = Path(worktree).resolve()
        self._cwd = Path(cwd).resolve()
        self._sample_limit = max(1, sample_limit)
        self._skills: dict[str, SkillInfo] = {}
        self._load()

    def list_skills(self) -> list[SkillInfo]:
        return list(self._skills.values())

    def get(self, name: str) -> SkillInfo | None:
        return self._skills.get(name)

    def sample_files(self, name: str, limit: int | None = None) -> list[str]:
        skill = self.get(name)
        if not skill:
            return []
        max_files = self._sample_limit if limit is None else max(1, limit)
        root = Path(skill.dir)
        files: list[str] = []
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if p.name == "SKILL.md":
                continue
            files.append(str(p.resolve()))
            if len(files) >= max_files:
                break
        return files

    def _load(self) -> None:
        for root in self._search_roots():
            for pattern in self.SEARCH_PATTERNS:
                for skill_md in sorted(root.glob(pattern)):
                    parsed = self._parse_skill(skill_md)
                    if not parsed:
                        continue
                    existing = self._skills.get(parsed.name)
                    if existing:
                        log.bind(
                            event="skill.duplicate",
                            skill=parsed.name,
                            first=existing.path,
                            duplicate=parsed.path,
                        ).warning("Duplicate skill name ignored")
                        continue
                    self._skills[parsed.name] = parsed

    def _search_roots(self) -> list[Path]:
        roots: list[Path] = []
        if not is_within(root=str(self._worktree), path=str(self._cwd)):
            return [self._worktree]

        cur = self._cwd
        while True:
            roots.append(cur)
            if cur == self._worktree:
                break
            parent = cur.parent
            if parent == cur:
                break
            cur = parent
        return roots

    def _parse_skill(self, skill_md: Path) -> SkillInfo | None:
        if not skill_md.is_file():
            return None

        try:
            content = skill_md.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            log.bind(event="skill.read.error", path=str(skill_md)).opt(exception=e).warning("Failed to read skill file")
            return None

        normalized = content.replace("\r\n", "\n")
        if normalized.startswith("\ufeff"):
            normalized = normalized.lstrip("\ufeff")

        m = _FRONTMATTER_RE.match(normalized)
        if not m:
            log.bind(event="skill.invalid", path=str(skill_md), reason="missing_frontmatter").warning(
                "Invalid skill file",
            )
            return None

        raw_frontmatter, body = m.groups()
        frontmatter = self._parse_frontmatter(raw_frontmatter)
        name = frontmatter.get("name", "")
        description = frontmatter.get("description", "")

        if not name:
            log.bind(event="skill.invalid", path=str(skill_md), reason="missing_name").warning("Invalid skill file")
            return None
        if not description or len(description) > 1024:
            log.bind(event="skill.invalid", path=str(skill_md), reason="invalid_description").warning("Invalid skill file")
            return None
        if not _SKILL_NAME_RE.fullmatch(name):
            log.bind(event="skill.invalid", path=str(skill_md), reason="invalid_name").warning("Invalid skill file")
            return None

        expected_name = skill_md.parent.name
        if name != expected_name:
            log.bind(
                event="skill.invalid",
                path=str(skill_md),
                reason="name_mismatch",
                expected=expected_name,
                actual=name,
            ).warning("Invalid skill file")
            return None

        return SkillInfo(
            name=name,
            description=description,
            path=str(skill_md.resolve()),
            dir=str(skill_md.parent.resolve()),
            body=body.strip(),
            raw_frontmatter=raw_frontmatter.strip(),
        )

    def _parse_frontmatter(self, raw: str) -> dict[str, str]:
        data: dict[str, str] = {}
        for line in raw.splitlines():
            if not line.strip():
                continue
            if line.startswith((" ", "\t")):
                continue
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip().strip("\"'")
        return data
