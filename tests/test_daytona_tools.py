from __future__ import annotations

import posixpath
import unittest
from dataclasses import dataclass, field
from fnmatch import fnmatch
from types import SimpleNamespace
from unittest.mock import patch

from chordcode.tools.daytona import (
    DaytonaBashTool,
    DaytonaCtx,
    DaytonaGlobTool,
    DaytonaGrepTool,
    DaytonaReadTool,
    DaytonaWriteTool,
)


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
            },
        )

    async def tool_stream_update(self, output: str) -> None:
        self.stream_updates.append(output)


class FakeManager:
    def __init__(self) -> None:
        self._rg_available: dict[str, bool] = {}
        self._rg_install_attempted: set[str] = set()

    def get_cached_rg_available(self, sandbox_id: str):
        return self._rg_available.get(sandbox_id)

    def set_cached_rg_available(self, sandbox_id: str, available: bool) -> None:
        self._rg_available[sandbox_id] = available

    def rg_install_attempted(self, sandbox_id: str) -> bool:
        return sandbox_id in self._rg_install_attempted

    def mark_rg_install_attempted(self, sandbox_id: str) -> None:
        self._rg_install_attempted.add(sandbox_id)


class FakeProcess:
    def __init__(self, responses: dict[str, tuple[int, str, str]]) -> None:
        self._responses = responses
        self.commands: list[str] = []

    def exec(self, command: str, cwd: str | None = None, timeout: int | None = None):  # noqa: ARG002
        self.commands.append(command)
        if command in self._responses:
            return self._responses[command]
        return (1, "", "")


class FakeFS:
    def __init__(
        self,
        files: dict[str, str],
        *,
        search_error: Exception | None = None,
        find_error: Exception | None = None,
    ) -> None:
        self._files = dict(files)
        self._folders: set[str] = {"/"}
        self.search_calls: list[tuple[str, str]] = []
        self.find_calls: list[tuple[str, str]] = []
        self._search_error = search_error
        self._find_error = find_error
        for p in files:
            cur = posixpath.dirname(p)
            while cur and cur != "/":
                self._folders.add(cur)
                cur = posixpath.dirname(cur)
        self._folders.add("/workspace")

    def download_file(self, path: str):
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]

    def upload_file(self, src, dst: str, timeout: int = 1800):  # noqa: ARG002
        if isinstance(src, bytes):
            data = src
        elif isinstance(src, str):
            data = src.encode("utf-8")
        else:
            raise TypeError("unsupported src")
        self._files[dst] = data.decode("utf-8")

    def create_folder(self, path: str, mode: str):  # noqa: ARG002
        self._folders.add(path)

    def list_files(self, path: str):
        path = posixpath.normpath(path)
        out: list[dict[str, object]] = []
        children: set[str] = set()
        for f in list(self._files):
            if not f.startswith(path.rstrip("/") + "/"):
                continue
            rel = f[len(path.rstrip("/") + "/") :]
            if not rel:
                continue
            first = rel.split("/", 1)[0]
            child = posixpath.join(path, first)
            children.add(child)
        for folder in list(self._folders):
            if folder == path:
                continue
            if not folder.startswith(path.rstrip("/") + "/"):
                continue
            rel = folder[len(path.rstrip("/") + "/") :]
            if not rel:
                continue
            first = rel.split("/", 1)[0]
            child = posixpath.join(path, first)
            children.add(child)
        for child in sorted(children):
            out.append(
                {
                    "path": child,
                    "name": posixpath.basename(child),
                    "is_dir": child in self._folders,
                },
            )
        return out

    def search_files(self, path: str, pattern: str):
        self.search_calls.append((path, pattern))
        if self._search_error:
            raise self._search_error

        root = posixpath.normpath(path)
        candidates = set(self._files.keys()) | set(self._folders)
        out: list[str] = []
        for candidate in sorted(candidates):
            if candidate == root:
                continue
            if not candidate.startswith(root.rstrip("/") + "/"):
                continue
            name = posixpath.basename(candidate)
            if fnmatch(name, pattern):
                out.append(candidate)
        return SimpleNamespace(files=out)

    def find_files(self, path: str, pattern: str):
        self.find_calls.append((path, pattern))
        if self._find_error:
            raise self._find_error

        root = posixpath.normpath(path)
        out: list[dict[str, object]] = []
        for file_path, content in sorted(self._files.items()):
            if not file_path.startswith(root.rstrip("/") + "/"):
                continue
            for idx, line in enumerate(content.splitlines(), start=1):
                if pattern in line:
                    out.append({"file": file_path, "line": idx, "content": line})
        return out


@dataclass
class FakeSandbox:
    process: FakeProcess
    fs: FakeFS


class DaytonaToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_bash_read_write_permissions(self) -> None:
        manager = FakeManager()
        sandbox = FakeSandbox(
            process=FakeProcess({"echo hi": (0, "hi\n", "")}),
            fs=FakeFS({"/workspace/a.txt": "hello\nworld\n"}),
        )
        ctx = DaytonaCtx(
            worktree="/workspace",
            cwd="/workspace",
            sandbox=sandbox,
            sandbox_id="sbx1",
            manager=manager,
        )
        tool_ctx = FakeToolCtx()

        bash_out = await DaytonaBashTool(ctx).execute({"command": "echo hi"}, tool_ctx)
        self.assertIn("hi", bash_out.output)
        self.assertEqual(tool_ctx.asks[0]["permission"], "bash")

        read_out = await DaytonaReadTool(ctx).execute({"file_path": "a.txt"}, tool_ctx)
        self.assertIn("hello", read_out.output)
        self.assertTrue(any(a["permission"] == "read" for a in tool_ctx.asks))

        await DaytonaWriteTool(ctx).execute({"file_path": "b.txt", "content": "ok"}, tool_ctx)
        self.assertEqual(sandbox.fs.download_file("/workspace/b.txt"), "ok")
        self.assertTrue(any(a["permission"] == "write" for a in tool_ctx.asks))

    async def test_write_creates_parent_folder_then_uploads_bytes(self) -> None:
        manager = FakeManager()
        sandbox = FakeSandbox(process=FakeProcess({}), fs=FakeFS({}))
        ctx = DaytonaCtx(
            worktree="/worktree",
            cwd="/worktree",
            sandbox=sandbox,
            sandbox_id="sbx2",
            manager=manager,
        )
        tool_ctx = FakeToolCtx()
        await DaytonaWriteTool(ctx).execute({"file_path": "/worktree/test.md", "content": "hello"}, tool_ctx)
        self.assertEqual(sandbox.fs.download_file("/worktree/test.md"), "hello")

    async def test_glob_uses_search_files(self) -> None:
        manager = FakeManager()
        files = {
            "/workspace/a.py": "print('a')\n",
            "/workspace/sub/b.py": "print('b')\n",
            "/workspace/c.txt": "x\n",
        }
        sandbox = FakeSandbox(process=FakeProcess({}), fs=FakeFS(files))
        ctx = DaytonaCtx(
            worktree="/workspace",
            cwd="/workspace",
            sandbox=sandbox,
            sandbox_id="sbx1",
            manager=manager,
        )
        tool_ctx = FakeToolCtx()
        out = await DaytonaGlobTool(ctx).execute({"pattern": "*.py", "path": "/workspace"}, tool_ctx)
        self.assertIn("/workspace/a.py", out.output)
        self.assertIn("/workspace/sub/b.py", out.output)
        self.assertEqual(sandbox.fs.search_calls, [("/workspace", "*.py")])

    async def test_glob_fallback_when_search_files_fails_and_warns(self) -> None:
        manager = FakeManager()
        files = {
            "/workspace/a.py": "print('a')\n",
            "/workspace/sub/b.py": "print('b')\n",
            "/workspace/c.txt": "x\n",
        }
        sandbox = FakeSandbox(
            process=FakeProcess({}),
            fs=FakeFS(files, search_error=RuntimeError("search unavailable")),
        )
        ctx = DaytonaCtx(
            worktree="/workspace",
            cwd="/workspace",
            sandbox=sandbox,
            sandbox_id="sbx1",
            manager=manager,
        )
        tool_ctx = FakeToolCtx()

        with patch("chordcode.tools.daytona.logger.warning") as warning_mock:
            out1 = await DaytonaGlobTool(ctx).execute({"pattern": "*.py"}, tool_ctx)
            out2 = await DaytonaGlobTool(ctx).execute({"pattern": "*.py"}, tool_ctx)

        self.assertIn("/workspace/a.py", out1.output)
        self.assertIn("/workspace/sub/b.py", out1.output)
        self.assertIn("/workspace/a.py", out2.output)

        events = [call.kwargs.get("event") for call in warning_mock.mock_calls]
        self.assertGreaterEqual(events.count("daytona.glob.search_files_fallback"), 2)

    async def test_grep_uses_find_files_with_include_filter(self) -> None:
        manager = FakeManager()
        files = {
            "/workspace/main.py": "hello from py\n",
            "/workspace/notes.txt": "hello from txt\n",
        }
        sandbox = FakeSandbox(process=FakeProcess({}), fs=FakeFS(files))
        ctx = DaytonaCtx(
            worktree="/workspace",
            cwd="/workspace",
            sandbox=sandbox,
            sandbox_id="sbx1",
            manager=manager,
        )
        tool_ctx = FakeToolCtx()
        out = await DaytonaGrepTool(ctx).execute({"pattern": "hello", "include": "*.py"}, tool_ctx)
        self.assertIn("Found 1 matches", out.output)
        self.assertIn("/workspace/main.py", out.output)
        self.assertNotIn("/workspace/notes.txt", out.output)
        self.assertEqual(sandbox.fs.find_calls, [("/workspace", "hello")])

    async def test_grep_fallback_without_find_files_and_without_rg(self) -> None:
        manager = FakeManager()
        process = FakeProcess(
            {
                "command -v rg": (1, "", ""),
                "apt-get update && apt-get install -y ripgrep": (1, "", ""),
                "apk add --no-cache ripgrep": (1, "", ""),
                "dnf install -y ripgrep": (1, "", ""),
                "yum install -y ripgrep": (1, "", ""),
                "microdnf install -y ripgrep": (1, "", ""),
                "pacman -Sy --noconfirm ripgrep": (1, "", ""),
            },
        )
        files = {
            "/workspace/main.py": "hello from py\n",
            "/workspace/notes.txt": "hello from txt\n",
        }
        sandbox = FakeSandbox(process=process, fs=FakeFS(files, find_error=RuntimeError("no find_files")))
        ctx = DaytonaCtx(
            worktree="/workspace",
            cwd="/workspace",
            sandbox=sandbox,
            sandbox_id="sbx1",
            manager=manager,
        )
        tool_ctx = FakeToolCtx()
        with patch("chordcode.tools.daytona.logger.warning") as warning_mock:
            out = await DaytonaGrepTool(ctx).execute({"pattern": "hello", "include": "*.py"}, tool_ctx)
        self.assertIn("Found 1 matches", out.output)
        self.assertIn("/workspace/main.py", out.output)
        self.assertNotIn("/workspace/notes.txt", out.output)
        events = [call.kwargs.get("event") for call in warning_mock.mock_calls]
        self.assertIn("daytona.grep.find_files_fallback", events)


if __name__ == "__main__":
    unittest.main()
