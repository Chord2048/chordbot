from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from chordcode.memory.manager import MemoryManager
from chordcode.tools.base import ToolResult


@dataclass(frozen=True)
class MemoryToolCtx:
    manager: MemoryManager


class MemorySearchTool:
    name = "memory_search"
    description = (
        "Search local workspace memory using hybrid BM25 and vector retrieval. "
        "Use this before answering questions about prior work, decisions, preferences, or todos."
    )

    def __init__(self, ctx: MemoryToolCtx) -> None:
        self._ctx = ctx

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                "min_score": {"type": "number", "default": 0.15, "minimum": 0, "maximum": 1},
            },
            "required": ["query"],
        }

    async def execute(self, args: dict[str, Any], ctx) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("query is required")
        max_results = max(1, min(int(args.get("max_results", 5) or 5), 20))
        min_score = float(args.get("min_score", 0.15) or 0.15)

        await ctx.ask(
            permission="memory_search",
            patterns=[query],
            always=["*"],
            metadata={"max_results": max_results, "min_score": min_score},
        )

        try:
            result = await self._ctx.manager.search(
                query=query,
                max_results=max_results,
                min_score=min_score,
            )
        except Exception as exc:
            result = {
                "hits": [],
                "error": str(exc),
                "stats": {
                    "worktree": self._ctx.manager.worktree,
                    "index_age_ms": self._ctx.manager.index_age_ms(),
                    "search_mode": "error",
                },
            }

        hits = result.get("hits", [])
        return ToolResult(
            title="Memory Search",
            output=json.dumps(result, ensure_ascii=False, indent=2),
            metadata={"query": query, "hit_count": len(hits) if isinstance(hits, list) else 0},
        )


class MemoryGetTool:
    name = "memory_get"
    description = (
        "Read raw content from memory.md or memory/*.md. "
        "Use this after memory_search when you need exact lines."
    )

    def __init__(self, ctx: MemoryToolCtx) -> None:
        self._ctx = ctx

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "memory.md or memory/*.md path"},
                "from_line": {"type": "integer", "default": 1, "minimum": 1},
                "max_lines": {"type": "integer", "default": 200, "minimum": 1, "maximum": 2000},
            },
            "required": ["path"],
        }

    async def execute(self, args: dict[str, Any], ctx) -> ToolResult:
        path = str(args.get("path", "")).strip()
        if not path:
            raise ValueError("path is required")
        from_line = max(1, int(args.get("from_line", 1) or 1))
        max_lines = max(1, min(int(args.get("max_lines", 200) or 200), 2000))

        await ctx.ask(
            permission="memory_get",
            patterns=[path],
            always=["*"],
            metadata={"from_line": from_line, "max_lines": max_lines},
        )

        try:
            result = await self._ctx.manager.read_file(
                path=path,
                from_line=from_line,
                max_lines=max_lines,
            )
        except Exception as exc:
            result = {
                "path": path,
                "from_line": from_line,
                "to_line": max(0, from_line - 1),
                "text": "",
                "error": str(exc),
            }

        return ToolResult(
            title="Memory Get",
            output=json.dumps(result, ensure_ascii=False, indent=2),
            metadata={"path": path, "from_line": from_line, "max_lines": max_lines},
        )
