from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from chordcode.kb.interface import KBBackend
from chordcode.log import logger
from chordcode.tools.base import ToolResult

_log = logger.child(service="tool.kb_search")


@dataclass(frozen=True)
class KBSearchCtx:
    kb: KBBackend


class KBSearchTool:
    name = "kb_search"
    description = (
        "Search the knowledge base for relevant information. "
        "Returns entities, relationships, and text chunks matching the query. "
        "Use this when the user asks about domain knowledge that may be stored in the KB."
    )

    def __init__(self, ctx: KBSearchCtx) -> None:
        self._ctx = ctx

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "top_k": {
                    "type": "integer",
                    "default": 10,
                    "description": "Max number of results to return",
                },
            },
            "required": ["query"],
        }

    async def execute(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("query is required")
        top_k = int(args.get("top_k", 10))

        _log.info("kb_search tool invoked", event="tool.kb_search.start", query=query, top_k=top_k)

        await ctx.ask(
            permission="kb_search",
            patterns=[query],
            always=["*"],
            metadata={"top_k": top_k},
        )

        t0 = time.monotonic()
        try:
            result = await self._ctx.kb.query(query=query, top_k=top_k)
        except Exception as exc:
            _log.error("kb_search query failed", event="tool.kb_search.error", query=query, error=str(exc))
            raise

        # Format readable output
        lines: list[str] = []

        if result.entities:
            lines.append(f"## Entities ({len(result.entities)})")
            for e in result.entities:
                entry = f"- **{e.entity_name}**"
                if e.entity_type:
                    entry += f" [{e.entity_type}]"
                if e.description:
                    entry += f": {e.description}"
                lines.append(entry)
            lines.append("")

        if result.relationships:
            lines.append(f"## Relationships ({len(result.relationships)})")
            for r in result.relationships:
                entry = f"- {r.src_id} -> {r.tgt_id}"
                if r.description:
                    entry += f": {r.description}"
                if r.keywords:
                    entry += f" (keywords: {r.keywords})"
                lines.append(entry)
            lines.append("")

        if result.chunks:
            lines.append(f"## Chunks ({len(result.chunks)})")
            for i, ch in enumerate(result.chunks, 1):
                header = f"### Chunk {i}"
                if ch.file_path:
                    header += f" (source: {ch.file_path})"
                lines.append(header)
                lines.append(ch.content)
                lines.append("")

        if not lines:
            lines.append("No results found for the query.")

        output = "\n".join(lines)

        elapsed = time.monotonic() - t0
        _log.info(
            "kb_search tool completed",
            event="tool.kb_search.done",
            query=query,
            entities=len(result.entities),
            relationships=len(result.relationships),
            chunks=len(result.chunks),
            output_len=len(output),
            elapsed_s=round(elapsed, 2),
        )

        return ToolResult(
            title="KB Search",
            output=output,
            metadata={
                "query": query,
                "top_k": top_k,
                "entity_count": len(result.entities),
                "relationship_count": len(result.relationships),
                "chunk_count": len(result.chunks),
            },
        )
