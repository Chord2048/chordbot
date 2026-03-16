from __future__ import annotations

from pathlib import Path

from chordcode.config import Config
from chordcode.hookdefs import Hook
from chordcode.memory.service import MemoryService
from chordcode.store.sqlite import SQLiteStore

_MAX_MEMORY_CONTEXT_CHARS = 8_000


def create_memory_hooks(*, cfg: Config, store: SQLiteStore, service: MemoryService):
    async def on_system_transform(input: dict, output: dict) -> None:
        if not service.enabled:
            return
        session_id = str(input.get("session_id") or "").strip()
        if not session_id:
            return
        try:
            session = await store.get_session(session_id)
        except KeyError:
            return
        if session.runtime.backend != "local":
            return

        system_parts = output.get("system")
        if not isinstance(system_parts, list):
            return

        worktree = Path(session.worktree)
        memory_file = worktree / "memory.md"
        if memory_file.is_file():
            content = memory_file.read_text(encoding="utf-8")
            truncated = False
            if len(content) > _MAX_MEMORY_CONTEXT_CHARS:
                content = content[:_MAX_MEMORY_CONTEXT_CHARS]
                truncated = True
            section = ["## Workspace Memory", content]
            if truncated:
                section.append("[memory.md truncated to 8000 characters]")
            system_parts.append("\n".join(section))

        system_parts.append(
            "\n".join(
                [
                    "## Memory Recall",
                    "Before answering questions about prior work, decisions, preferences, or todos, use memory_search on local memory.",
                    "Use memory_get only after memory_search when you need exact lines or larger raw context.",
                    "When you need to update long-term memory, rewrite memory.md or append dated notes to memory/YYYY-MM-DD.md using the existing read and write tools.",
                    "Put stable evergreen facts in memory.md, and append dated session conclusions to memory/YYYY-MM-DD.md.",
                ]
            )
        )

    return {Hook.ExperimentalChatSystemTransform: on_system_transform}
