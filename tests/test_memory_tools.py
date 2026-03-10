from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "src"))

from chordcode.config import MemoryConfig  # noqa: E402
from chordcode.memory.manager import MemoryManager, build_memory_db_path  # noqa: E402
from chordcode.tools.memory import MemoryGetTool, MemorySearchTool, MemoryToolCtx  # noqa: E402


@dataclass
class FakeToolExecCtx:
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
        return None


class FakeSearchManager:
    worktree = "/tmp/worktree"

    async def search(self, *, query: str, max_results: int = 5, min_score: float = 0.15):
        return {
            "hits": [
                {
                    "path": "memory.md",
                    "start_line": 1,
                    "end_line": 2,
                    "score": 0.9,
                    "snippet": f"hit for {query}",
                    "source": "memory",
                }
            ],
            "stats": {"worktree": self.worktree, "index_age_ms": 10, "search_mode": "hybrid"},
        }

    async def read_file(self, *, path: str, from_line: int = 1, max_lines: int = 200):
        return {"path": path, "from_line": from_line, "to_line": from_line + max_lines - 1, "text": "sample"}

    def index_age_ms(self) -> int:
        return 10


class MemoryToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_search_returns_json_output(self) -> None:
        tool = MemorySearchTool(MemoryToolCtx(manager=FakeSearchManager()))
        ctx = FakeToolExecCtx()

        out = await tool.execute({"query": "prior decision", "max_results": 3, "min_score": 0.2}, ctx)
        payload = json.loads(out.output)

        self.assertEqual(out.title, "Memory Search")
        self.assertEqual(payload["hits"][0]["path"], "memory.md")
        self.assertEqual(ctx.asks[0]["permission"], "memory_search")
        self.assertEqual(ctx.asks[0]["patterns"], ["prior decision"])

    async def test_memory_get_reads_exact_line_slice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            (worktree / "memory").mkdir()
            (worktree / "memory" / "2026-03-10.md").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
            manager = MemoryManager(
                worktree=tmp,
                config=MemoryConfig(sync_interval_seconds=3600),
                db_path=build_memory_db_path(str(worktree / "app.sqlite3"), tmp),
                embedding_provider=None,
            )
            await manager.init()
            tool = MemoryGetTool(MemoryToolCtx(manager=manager))
            ctx = FakeToolExecCtx()

            out = await tool.execute({"path": "memory/2026-03-10.md", "from_line": 2, "max_lines": 2}, ctx)
            payload = json.loads(out.output)

            self.assertEqual(payload["from_line"], 2)
            self.assertEqual(payload["to_line"], 3)
            self.assertEqual(payload["text"], "two\nthree")
            self.assertEqual(ctx.asks[0]["permission"], "memory_get")

    async def test_memory_get_rejects_non_memory_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            manager = MemoryManager(
                worktree=tmp,
                config=MemoryConfig(sync_interval_seconds=3600),
                db_path=build_memory_db_path(str(worktree / "app.sqlite3"), tmp),
                embedding_provider=None,
            )
            await manager.init()
            tool = MemoryGetTool(MemoryToolCtx(manager=manager))
            ctx = FakeToolExecCtx()

            out = await tool.execute({"path": "notes.md"}, ctx)
            payload = json.loads(out.output)

            self.assertIn("error", payload)
            self.assertIn("memory.md or under memory/", payload["error"])
