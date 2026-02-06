from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tree_sitter import Language, Parser
from tree_sitter_bash import language as bash_language

from chordcode.tools.base import ToolResult
from chordcode.tools.paths import is_within
from chordcode.tools.truncate import truncate


@dataclass(frozen=True)
class BashCtx:
    worktree: str
    cwd: str


def _parser() -> Parser:
    p = Parser()
    cap = bash_language()
    lang = cap if isinstance(cap, Language) else Language(cap)
    if hasattr(p, "set_language"):
        p.set_language(lang)
    else:
        p.language = lang
    return p


def _tokens(src: bytes, node) -> list[str]:
    out: list[str] = []
    for i in range(node.child_count):
        c = node.child(i)
        if not c:
            continue
        if c.type in {"command_name", "word", "string", "raw_string", "concatenation"}:
            out.append(src[c.start_byte : c.end_byte].decode("utf-8", errors="replace"))
    return out


def _commands(src: bytes, tree) -> list[list[str]]:
    root = tree.root_node
    out: list[list[str]] = []
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "command":
            t = _tokens(src, n)
            if t:
                out.append(t)
        for i in range(n.child_count - 1, -1, -1):
            c = n.child(i)
            if c:
                stack.append(c)
    return out


class BashTool:
    name = "bash"
    description = "Execute a shell command within the session context."

    def __init__(self, ctx: BashCtx) -> None:
        self._ctx = ctx
        self._p = _parser()

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "workdir": {"type": "string"},
                "timeout_ms": {"type": "integer"},
                "description": {"type": "string"},
            },
            "required": ["command"],
        }

    async def execute(self, args: dict[str, Any], ctx) -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command is required")

        workdir = str(args.get("workdir") or self._ctx.cwd)
        timeout_ms = int(args.get("timeout_ms") or 120_000)
        if timeout_ms < 0:
            raise ValueError("timeout_ms must be >= 0")

        if not is_within(root=self._ctx.worktree, path=workdir):
            await ctx.ask(
                permission="external_directory",
                patterns=[workdir],
                always=[str(Path(workdir).parent) + "/*"],
                metadata={},
            )

        src = command.encode("utf-8", errors="replace")
        tree = self._p.parse(src)
        cmds = _commands(src, tree)
        patterns = [" ".join(c) for c in cmds if c]
        always = []
        for c in cmds:
            if not c:
                continue
            always.append(c[0] + "*")
        if patterns:
            await ctx.ask(permission="bash", patterns=patterns, always=always or ["*"], metadata={"workdir": workdir})

        path_cmds = {"cd", "rm", "cp", "mv", "mkdir", "touch", "chmod", "chown", "cat"}
        for c in cmds:
            if not c:
                continue
            if c[0] not in path_cmds:
                continue
            for a in c[1:]:
                if a.startswith("-"):
                    continue
                if c[0] == "chmod" and a.startswith("+"):
                    continue
                try:
                    resolved = str((Path(workdir) / a).resolve())
                except Exception:
                    continue
                if is_within(root=self._ctx.worktree, path=resolved):
                    continue
                await ctx.ask(
                    permission="external_directory",
                    patterns=[resolved],
                    always=[str(Path(resolved).parent) + "/*"],
                    metadata={"command": c[0]},
                )

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        buf: list[str] = []

        async def pump(stream):
            while True:
                chunk = await stream.readline()
                if not chunk:
                    return
                buf.append(chunk.decode("utf-8", errors="replace"))
                t = truncate("".join(buf))
                await ctx.tool_stream_update(t.content)

        tasks = []
        if proc.stdout:
            tasks.append(asyncio.create_task(pump(proc.stdout)))
        if proc.stderr:
            tasks.append(asyncio.create_task(pump(proc.stderr)))

        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_ms / 1000)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"bash timed out after {timeout_ms}ms")
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

        out = "".join(buf)
        t = truncate(out)
        code = proc.returncode
        return ToolResult(
            title="bash",
            output=t.content,
            metadata={"returncode": code, "truncated": t.truncated, "workdir": workdir},
        )
