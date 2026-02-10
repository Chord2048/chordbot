from __future__ import annotations

from typing import Any

from chordcode.log import logger
from chordcode.mcp.client import MCPManager, MCPToolInfo
from chordcode.tools.base import ToolContext, ToolResult

_log = logger.child(service="mcp")


class MCPToolAdapter:
    """Wraps an MCP tool as a built-in Tool Protocol implementation."""

    def __init__(self, info: MCPToolInfo, manager: MCPManager) -> None:
        self._info = info
        self._manager = manager
        self.name: str = info.namespaced_name
        self.description: str = f"[MCP:{info.server_name}] {info.description}"

    def schema(self) -> dict[str, Any]:
        return self._info.input_schema

    async def execute(
        self, args: dict[str, Any], ctx: ToolContext,
    ) -> ToolResult:
        server = self._info.server_name
        tool = self._info.tool_name

        # Permission gate
        await ctx.ask(
            permission="mcp",
            patterns=[self.name],
            always=[f"{server}_*"],
            metadata={
                "mcp_server": server,
                "mcp_tool": tool,
            },
        )

        try:
            result = await self._manager.call_tool(server, tool, args)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            _log.warning(
                f"mcp tool {self.name} exception: {err}",
                event="mcp.tool.error",
                tool_name=self.name,
            )
            return ToolResult(
                title=f"MCP:{server}/{tool}",
                output=f"Error: {err}",
                metadata={"mcp_server": server, "mcp_tool": tool},
            )

        if result.is_error:
            return ToolResult(
                title=f"MCP:{server}/{tool}",
                output=f"Error: {result.content}",
                metadata={"mcp_server": server, "mcp_tool": tool},
            )

        return ToolResult(
            title=f"MCP:{server}/{tool}",
            output=result.content,
            metadata={"mcp_server": server, "mcp_tool": tool},
        )
