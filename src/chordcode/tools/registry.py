from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from chordcode.tools.base import Tool


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str
    schema: dict[str, Any]
    tool: Tool


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {t.name: t for t in tools}

    def list(self) -> list[ToolInfo]:
        out: list[ToolInfo] = []
        for t in self._tools.values():
            out.append(ToolInfo(name=t.name, description=t.description, schema=t.schema(), tool=t))
        return out

    def get(self, name: str) -> Tool:
        t = self._tools.get(name)
        if not t:
            raise KeyError(f"unknown tool: {name}")
        return t

