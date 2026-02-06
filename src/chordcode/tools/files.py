from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chordcode.tools.base import ToolResult
from chordcode.tools.paths import is_within, resolve_path
from chordcode.tools.truncate import truncate


@dataclass(frozen=True)
class FileCtx:
    worktree: str
    cwd: str


class ReadTool:
    name = "read"
    description = "Read a text file from the session worktree."

    def __init__(self, ctx: FileCtx) -> None:
        self._ctx = ctx

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["file_path"],
        }

    async def execute(self, args: dict[str, Any], ctx) -> ToolResult:
        file_path = str(args.get("file_path", ""))
        if not file_path:
            raise ValueError("file_path is required")
        path = resolve_path(cwd=self._ctx.cwd, file_path=file_path)
        if not is_within(root=self._ctx.worktree, path=path):
            await ctx.ask(permission="external_directory", patterns=[path], always=[str(Path(path).parent) + "/*"], metadata={})

        await ctx.ask(permission="read", patterns=[path], always=["*"], metadata={})

        p = Path(path)
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(path)

        offset = int(args.get("offset") or 0)
        limit = int(args.get("limit") or 2000)
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        chunk = lines[offset : offset + limit]
        text = "\n".join(chunk)
        t = truncate(text)
        return ToolResult(
            title=str(p),
            output=t.content,
            metadata={"truncated": t.truncated, "offset": offset, "limit": limit, "total_lines": len(lines)},
        )


class WriteTool:
    name = "write"
    description = "Write a text file under the session worktree."

    def __init__(self, ctx: FileCtx) -> None:
        self._ctx = ctx

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
        }

    async def execute(self, args: dict[str, Any], ctx) -> ToolResult:
        file_path = str(args.get("file_path", ""))
        content = str(args.get("content", ""))
        if not file_path:
            raise ValueError("file_path is required")

        path = resolve_path(cwd=self._ctx.cwd, file_path=file_path)
        if not is_within(root=self._ctx.worktree, path=path):
            await ctx.ask(permission="external_directory", patterns=[path], always=[str(Path(path).parent) + "/*"], metadata={})

        await ctx.ask(permission="write", patterns=[path], always=["*"], metadata={})

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ToolResult(title=str(p), output="Wrote file successfully.", metadata={"file_path": str(p)})

