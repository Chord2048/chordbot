from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from chordcode.tools.bash import BashCtx, BashTool
from chordcode.tools.files import FileCtx, ReadTool, WriteTool


@dataclass
class FakeToolCtx:
    session_id: str = "s1"
    message_id: str = "m1"
    agent: str = "primary"
    asks: list[dict] = field(default_factory=list)
    stream_updates: list[str] = field(default_factory=list)

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
        self.stream_updates.append(output)


class ToolPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_requests_read_permission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.txt"
            p.write_text("hello\nworld\n", encoding="utf-8")
            tool = ReadTool(FileCtx(worktree=tmp, cwd=tmp))
            ctx = FakeToolCtx()

            out = await tool.execute({"file_path": "a.txt"}, ctx)
            self.assertIn("hello", out.output)
            self.assertEqual(len(ctx.asks), 1)
            self.assertEqual(ctx.asks[0]["permission"], "read")
            self.assertEqual(ctx.asks[0]["patterns"], [str(p.resolve())])

    async def test_read_requests_external_directory_then_read_when_outside_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as worktree, tempfile.TemporaryDirectory() as other:
            p = Path(other) / "x.txt"
            p.write_text("x", encoding="utf-8")

            tool = ReadTool(FileCtx(worktree=worktree, cwd=worktree))
            ctx = FakeToolCtx()
            await tool.execute({"file_path": str(p)}, ctx)

            self.assertGreaterEqual(len(ctx.asks), 2)
            self.assertEqual(ctx.asks[0]["permission"], "external_directory")
            self.assertEqual(ctx.asks[0]["patterns"], [str(p)])
            self.assertEqual(ctx.asks[1]["permission"], "read")
            self.assertEqual(ctx.asks[1]["patterns"], [str(p)])

    async def test_write_requests_external_directory_then_write_when_outside_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as worktree, tempfile.TemporaryDirectory() as other:
            p = Path(other) / "out.txt"
            tool = WriteTool(FileCtx(worktree=worktree, cwd=worktree))
            ctx = FakeToolCtx()

            await tool.execute({"file_path": str(p), "content": "ok"}, ctx)
            self.assertTrue(p.exists())
            self.assertEqual(p.read_text(encoding="utf-8"), "ok")

            self.assertGreaterEqual(len(ctx.asks), 2)
            self.assertEqual(ctx.asks[0]["permission"], "external_directory")
            self.assertEqual(ctx.asks[0]["patterns"], [str(p)])
            self.assertEqual(ctx.asks[1]["permission"], "write")
            self.assertEqual(ctx.asks[1]["patterns"], [str(p)])

    async def test_bash_requests_bash_permission_with_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as worktree:
            tool = BashTool(BashCtx(worktree=worktree, cwd=worktree))
            ctx = FakeToolCtx()
            out = await tool.execute({"command": "echo hi"}, ctx)
            self.assertIn("hi", out.output)

            self.assertGreaterEqual(len(ctx.asks), 1)
            self.assertEqual(ctx.asks[0]["permission"], "bash")
            self.assertEqual(ctx.asks[0]["patterns"], ["echo hi"])
            self.assertIn("echo*", ctx.asks[0]["always"])

    async def test_bash_flags_external_directory_for_path_commands(self) -> None:
        with tempfile.TemporaryDirectory() as worktree:
            tool = BashTool(BashCtx(worktree=worktree, cwd=worktree))
            ctx = FakeToolCtx()
            await tool.execute({"command": "cd /tmp"}, ctx)

            perms = [a["permission"] for a in ctx.asks]
            self.assertIn("bash", perms)
            self.assertIn("external_directory", perms)
