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

    async def ask(self, *, permission: str, patterns: list[str], always: list[str], metadata: dict[str, Any]) -> None: ...
    async def tool_stream_update(self, output: str) -> None: ...


class Tool(Protocol):
    name: str
    description: str

    def schema(self) -> dict[str, Any]: ...
    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...
