from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolResult:
    title: str
    output: str
    metadata: dict[str, Any]


class ToolContext(Protocol):
    session_id: str
    message_id: str
    agent: str
    source: str
    tool_part_id: str
    trace_id: str | None
    parent_observation_id: str | None
    root_session_id: str | None
    parent_session_id: str | None
    parallel_group_id: str | None
    parallel_index: int | None
    parallel_size: int | None

    async def ask(self, *, permission: str, patterns: list[str], always: list[str], metadata: dict[str, Any]) -> None: ...
    async def tool_stream_update(self, output: str) -> None: ...


class Tool(Protocol):
    name: str
    description: str

    def schema(self) -> dict[str, Any]: ...
    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...
