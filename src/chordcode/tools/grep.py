from __future__ import annotations

import asyncio
import fnmatch
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chordcode.tools.base import ToolResult
from chordcode.tools.paths import is_within, resolve_path


_MAX_RESULTS = 100
_MAX_LINE_LENGTH = 2000


@dataclass(frozen=True)
class SearchCtx:
    worktree: str
    cwd: str


@dataclass(frozen=True)
class _Match:
    path: str
    mtime: float
    line_num: int
    line_text: str


async def _ask_external_directory_if_needed(*, ctx, worktree: str, path: str, metadata: dict[str, Any]) -> None:
    if is_within(root=worktree, path=path):
        return
    await ctx.ask(
        permission="external_directory",
        patterns=[path],
        always=[str(Path(path).parent) + "/*"],
        metadata=metadata,
    )


def _title_for_search_root(*, search_root: str, worktree: str) -> str:
    root = Path(search_root).resolve()
    wt = Path(worktree).resolve()
    try:
        rel = root.relative_to(wt)
        return "." if str(rel) == "" else str(rel)
    except Exception:
        return str(root)


def _matches_glob(pattern: str, rel_path: str) -> bool:
    rel_posix = rel_path.replace("\\", "/")
    name = Path(rel_posix).name
    if fnmatch.fnmatch(rel_posix, pattern):
        return True
    if fnmatch.fnmatch(name, pattern):
        return True
    if pattern.startswith("./") and fnmatch.fnmatch(rel_posix, pattern[2:]):
        return True
    return False


class GlobTool:
    name = "glob"
    description = "Find files by glob pattern under a directory (defaults to current working directory)."

    def __init__(self, ctx: SearchCtx) -> None:
        self._ctx = ctx

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern to match files"},
                "path": {"type": "string", "description": "Directory to search in (defaults to current working directory)"},
            },
            "required": ["pattern"],
        }

    async def execute(self, args: dict[str, Any], ctx) -> ToolResult:
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern is required")

        raw_path = str(args.get("path") or "").strip() or self._ctx.cwd
        search_root = resolve_path(cwd=self._ctx.cwd, file_path=raw_path)

        root_path = Path(search_root)
        if not root_path.exists() or not root_path.is_dir():
            raise NotADirectoryError(search_root)

        await _ask_external_directory_if_needed(
            ctx=ctx,
            worktree=self._ctx.worktree,
            path=search_root,
            metadata={"tool": "glob"},
        )
        await ctx.ask(
            permission="glob",
            patterns=[pattern],
            always=["*"],
            metadata={"pattern": pattern, "path": args.get("path")},
        )

        files, truncated = await _glob_with_rg(search_root=search_root, pattern=pattern)
        if files is None:
            files, truncated = await _glob_fallback(search_root=search_root, pattern=pattern)

        output_lines: list[str] = []
        if not files:
            output_lines.append("No files found")
        else:
            output_lines.extend(files)
            if truncated:
                output_lines.append("")
                output_lines.append("(Results are truncated. Consider using a more specific path or pattern.)")

        return ToolResult(
            title=_title_for_search_root(search_root=search_root, worktree=self._ctx.worktree),
            output="\n".join(output_lines),
            metadata={"count": len(files), "truncated": truncated},
        )


class GrepTool:
    name = "grep"
    description = "Search file contents with a regex pattern under a directory (defaults to current working directory)."

    def __init__(self, ctx: SearchCtx) -> None:
        self._ctx = ctx

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {"type": "string", "description": "Directory to search in (defaults to current working directory)"},
                "include": {"type": "string", "description": 'File include glob (e.g. "*.py", "*.{ts,tsx}")'},
            },
            "required": ["pattern"],
        }

    async def execute(self, args: dict[str, Any], ctx) -> ToolResult:
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern is required")

        include_value = args.get("include")
        include = str(include_value).strip() if include_value is not None else None
        if include == "":
            include = None

        raw_path = str(args.get("path") or "").strip() or self._ctx.cwd
        search_root = resolve_path(cwd=self._ctx.cwd, file_path=raw_path)

        root_path = Path(search_root)
        if not root_path.exists() or not root_path.is_dir():
            raise NotADirectoryError(search_root)

        await _ask_external_directory_if_needed(
            ctx=ctx,
            worktree=self._ctx.worktree,
            path=search_root,
            metadata={"tool": "grep"},
        )
        await ctx.ask(
            permission="grep",
            patterns=[pattern],
            always=["*"],
            metadata={"pattern": pattern, "path": args.get("path"), "include": include},
        )

        matches, has_errors = await _grep_with_rg(search_root=search_root, pattern=pattern, include=include)
        if matches is None:
            matches, has_errors = await _grep_fallback(search_root=search_root, pattern=pattern, include=include)

        if not matches:
            return ToolResult(
                title=pattern,
                output="No files found",
                metadata={"matches": 0, "truncated": False},
            )

        truncated = len(matches) > _MAX_RESULTS
        final_matches = matches[:_MAX_RESULTS]

        output_lines: list[str] = [f"Found {len(final_matches)} matches"]
        current_file = ""
        for match in final_matches:
            if current_file != match.path:
                if current_file:
                    output_lines.append("")
                current_file = match.path
                output_lines.append(f"{match.path}:")
            line_text = match.line_text
            if len(line_text) > _MAX_LINE_LENGTH:
                line_text = line_text[:_MAX_LINE_LENGTH] + "..."
            output_lines.append(f"  Line {match.line_num}: {line_text}")

        if truncated:
            output_lines.append("")
            output_lines.append("(Results are truncated. Consider using a more specific path or pattern.)")
        if has_errors:
            output_lines.append("")
            output_lines.append("(Some paths were inaccessible and skipped)")

        return ToolResult(
            title=pattern,
            output="\n".join(output_lines),
            metadata={"matches": len(final_matches), "truncated": truncated},
        )


async def _glob_with_rg(*, search_root: str, pattern: str) -> tuple[list[str], bool] | tuple[None, bool]:
    rg = shutil.which("rg")
    if not rg:
        return None, False

    proc = await asyncio.create_subprocess_exec(
        rg,
        "--files",
        "--hidden",
        "--no-messages",
        "--glob",
        pattern,
        search_root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    exit_code = proc.returncode or 0

    if exit_code not in {0, 1}:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ripgrep failed: {err or f'exit code {exit_code}'}")

    candidates: list[tuple[str, float]] = []
    for raw in stdout.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        p = Path(line)
        if not p.is_absolute():
            p = Path(search_root) / p
        try:
            resolved = str(p.resolve())
            mtime = Path(resolved).stat().st_mtime
        except Exception:
            continue
        candidates.append((resolved, mtime))

    candidates.sort(key=lambda item: item[1], reverse=True)
    truncated = len(candidates) > _MAX_RESULTS
    files = [item[0] for item in candidates[:_MAX_RESULTS]]
    return files, truncated


async def _glob_fallback(*, search_root: str, pattern: str) -> tuple[list[str], bool]:
    root = Path(search_root)
    candidates: list[tuple[str, float]] = []
    had_errors = False

    try:
        iterator = root.rglob("*")
        for item in iterator:
            try:
                if not item.is_file():
                    continue
                rel = item.relative_to(root).as_posix()
                if not _matches_glob(pattern, rel):
                    continue
                stat = item.stat()
                candidates.append((str(item.resolve()), stat.st_mtime))
            except Exception:
                had_errors = True
                continue
    except Exception:
        had_errors = True

    candidates.sort(key=lambda item: item[1], reverse=True)
    truncated = len(candidates) > _MAX_RESULTS
    files = [item[0] for item in candidates[:_MAX_RESULTS]]
    if had_errors and not files:
        return [], False
    return files, truncated


async def _grep_with_rg(*, search_root: str, pattern: str, include: str | None) -> tuple[list[_Match], bool] | tuple[None, bool]:
    rg = shutil.which("rg")
    if not rg:
        return None, False

    args = [
        rg,
        "-nH",
        "--hidden",
        "--no-messages",
        "--field-match-separator=|",
        "--regexp",
        pattern,
    ]
    if include:
        args.extend(["--glob", include])
    args.append(search_root)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    exit_code = proc.returncode or 0

    output = stdout.decode("utf-8", errors="replace")
    error_output = stderr.decode("utf-8", errors="replace")
    if exit_code == 1 or (exit_code == 2 and not output.strip()):
        return [], False
    if exit_code not in {0, 2}:
        raise RuntimeError(f"ripgrep failed: {error_output.strip() or f'exit code {exit_code}'}")

    has_errors = exit_code == 2
    matches: list[_Match] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue

        parts = line.split("|")
        if len(parts) < 3:
            continue

        file_path, line_num_text, *line_text_parts = parts
        try:
            line_num = int(line_num_text)
        except ValueError:
            continue
        if not line_text_parts:
            continue

        p = Path(file_path)
        if not p.is_absolute():
            p = Path(search_root) / p
        try:
            resolved = str(p.resolve())
            mtime = Path(resolved).stat().st_mtime
        except Exception:
            continue

        matches.append(
            _Match(
                path=resolved,
                mtime=mtime,
                line_num=line_num,
                line_text="|".join(line_text_parts),
            )
        )

    matches.sort(key=lambda m: m.mtime, reverse=True)
    return matches, has_errors


async def _grep_fallback(*, search_root: str, pattern: str, include: str | None) -> tuple[list[_Match], bool]:
    try:
        regex = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"invalid regex pattern: {e}") from e

    root = Path(search_root)
    matches: list[_Match] = []
    has_errors = False

    try:
        iterator = root.rglob("*")
        for item in iterator:
            try:
                if not item.is_file():
                    continue
                rel = item.relative_to(root).as_posix()
                if include and not _matches_glob(include, rel):
                    continue
                mtime = item.stat().st_mtime
                with item.open("r", encoding="utf-8", errors="replace") as f:
                    for idx, text in enumerate(f, start=1):
                        clean = text.rstrip("\r\n")
                        if regex.search(clean):
                            matches.append(
                                _Match(
                                    path=str(item.resolve()),
                                    mtime=mtime,
                                    line_num=idx,
                                    line_text=clean,
                                )
                            )
            except Exception:
                has_errors = True
                continue
    except Exception:
        has_errors = True

    matches.sort(key=lambda m: m.mtime, reverse=True)
    return matches, has_errors
